"""Bazel backend: wraps the existing, unchanged root Bazel build (see
BUILD.bazel, MODULE.bazel, .bazelrc) as a BuildBackend. Never modifies any
existing Bazel file -- only shells out to `bazel build`/`bazel cquery`
against the targets ci/profiles.yaml's bazel profile already names.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict

from base import BackendError, BuildBackend, BuildResult, EnvironmentCheck, TargetResult

#: Bazel labels that produce an executable rather than a shared library --
#: everything else in a profile's `targets` map is treated as a library.
_EXECUTABLE_TARGETS = {"consumer"}


def _extract_printed_labels(label_kind_output: str) -> set:
    """The exact set of `//...`/`@...` label tokens `bazel cquery
    --output=label_kind` actually printed a line for -- one label per
    resolved-target line, `<kind> rule <label> (<config-hash>)` (verified
    against a real `bazel cquery` run: "cc_binary rule //:math
    (9bbc831)"), interleaved with diagnostic lines ("Loading: ...",
    "INFO: ..."). A pure function (no subprocess) so it's directly unit
    testable without mocking `_run`, which this codebase's own convention
    (see BuildBackend._run's docstring) keeps un-mocked everywhere else.
    """
    printed_labels = set()
    for line in label_kind_output.splitlines():
        for token in line.split():
            if token.startswith("//") or token.startswith("@"):
                printed_labels.add(token)
    return printed_labels


class BazelBackend(BuildBackend):
    name = "bazel"

    def _bazel_executable(self) -> str:
        return shutil.which("bazel") or "bazel"

    def verify_environment(self) -> EnvironmentCheck:
        exe = self._bazel_executable()
        version = self._tool_version(exe, "version")
        if version is None:
            return EnvironmentCheck(ok=False, missing=[exe], notes=["bazel not found on PATH"])
        tool_versions = {"bazel": version}
        missing = []
        # build() now passes CC/CXX via --repo_env (_toolchain_args()), making
        # the declared compiler a hard build dependency rather than just
        # descriptive metadata -- verify it here too, or a runner missing
        # gcc-14/g++-14 reports ok=True and only fails later, mid-build
        # (Codex review, PR #19).
        compiler = self.profile.get("compiler", {})
        for role in ("cc", "cxx"):
            tool = compiler.get(role)
            if not tool:
                continue
            tool_version = self._tool_version(tool)
            if tool_version is None:
                missing.append(tool)
            else:
                tool_versions[role] = tool_version
        # stage() requires patchelf to give the staged consumer_app a
        # RUNPATH that resolves outside Bazel's own bazel-bin/_solib_*/
        # tree (see stage()'s own comment) -- check it here too so a
        # runner missing it reports ok=False instead of only failing (now
        # closed, not silently) once staging runs (Codex review, PR #19
        # follow-up).
        patchelf_version = self._tool_version("patchelf")
        if patchelf_version is None:
            missing.append("patchelf")
        else:
            tool_versions["patchelf"] = patchelf_version
        if missing:
            return EnvironmentCheck(ok=False, missing=missing, tool_versions=tool_versions)
        return EnvironmentCheck(ok=True, tool_versions=tool_versions)

    def clean(self) -> None:
        # Deliberately NOT `bazel clean --expunge` here: that would defeat
        # the disk cache abi-scan.yml/baseline.yml already rely on for
        # every OTHER job sharing this checkout. This backend's `build()`
        # already reflects real, current source state on every run (Bazel
        # itself is the incremental cache), so there is nothing profile-
        # specific to clean between runs.
        return None

    def configure(self) -> str:
        return ""  # Bazel has no separate configure step.

    def _labels(self) -> Dict[str, str]:
        return dict(self.profile["targets"])

    def _toolchain_args(self) -> list:
        # ci/profiles.yaml's compiler.cc/cxx previously named intent only
        # ("Bazel resolves its own C++ toolchain independently of $PATH")
        # while `bazel build` ran with no CC/CXX override at all -- meaning
        # a bare `bazel build` here actually used whatever `cc`/`gcc`
        # resolves to by default (e.g. gcc-13 on ubuntu-24.04, the
        # non-versioned default), while emit_build_output.py's provenance
        # unconditionally reported the *declared* gcc-14/g++-14 version
        # instead of what was actually used to produce the shipped binary
        # (Codex review, PR #19). Bazel's autoconfigured cc toolchain reads
        # CC/CXX from the repository rule's environment, so pass them as
        # --repo_env to make the declared compiler the one Bazel truly
        # invokes, closing the intent/reality gap instead of just
        # correcting the report.
        compiler = self.profile.get("compiler", {})
        args = []
        if compiler.get("cc"):
            args.append(f"--repo_env=CC={compiler['cc']}")
        if compiler.get("cxx"):
            args.append(f"--repo_env=CXX={compiler['cxx']}")
        return args

    def build(self) -> BuildResult:
        started = time.time()
        labels = self._labels()
        diagnostics = []
        build_log = ""
        success = True
        try:
            proc = self._run(
                [self._bazel_executable(), "build", *self._toolchain_args(), *labels.values()]
            )
            build_log = proc.stdout
        except BackendError as exc:
            build_log = str(exc)
            success = False
            diagnostics.append(f"bazel build failed: {exc}")

        targets: Dict[str, TargetResult] = {}
        for name, label in labels.items():
            kind = "executable" if name in _EXECUTABLE_TARGETS else "shared_library"
            path = None
            if success:
                try:
                    path = self._resolve_output_path(label)
                except BackendError as exc:
                    diagnostics.append(f"could not resolve output path for {name!r} ({label}): {exc}")
            result = TargetResult.from_path(name, kind, path)
            if not result.built:
                success = False
                diagnostics.append(f"target {name!r} ({label}) produced no output")
            targets[name] = result

        return BuildResult(
            profile_id=self.profile["id"],
            backend=self.name,
            success=success,
            started_at=started,
            ended_at=time.time(),
            targets=targets,
            diagnostics=diagnostics,
            configure_log="",
            build_log=build_log,
        )

    def _resolve_output_path(self, label: str) -> Path:
        proc = self._run(
            [self._bazel_executable(), "cquery", "--output=files", label], check=False
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("INFO:") and not line.startswith("Loading"):
                candidate = (self.repo_root / line).resolve()
                if candidate.is_file():
                    return candidate
        raise BackendError(f"could not resolve build output path for {label}")

    def collect_evidence(self, build_result: BuildResult) -> Dict[str, Any]:
        labels = list(self._labels().values())
        # "targets" previously defaulted to the configured labels
        # unconditionally, before cquery ever ran -- ci/check_profile_coverage.py's
        # resolved_target_count reads this list directly, so a cquery
        # failure (or one that silently returned nothing) still reported
        # all 4 targets "resolved" and the coverage contract passed with
        # no real evidence behind it (Codex review, PR #20). Only labels
        # cquery actually printed a line for now count as resolved.
        evidence: Dict[str, Any] = {"kind": "bazel-cquery", "targets": []}
        try:
            # `cquery` takes ONE query expression, not N positional target
            # args -- passing labels as separate argv entries (as this line
            # did before) makes bazel reject it outright ("unexpected token
            # ... after query expression"). Under check=False that failure
            # was silently absorbed and this method still reported evidence
            # for all 4 configured targets regardless (the bug the resolved-
            # targets fix above closes) -- meaning cquery evidence had never
            # actually been collected here. Join with `+` (set union) for
            # one valid query expression.
            proc = self._run(
                [self._bazel_executable(), "cquery", "--output=label_kind", " + ".join(labels)],
                check=False,
            )
            evidence["label_kind"] = proc.stdout.strip()
            if proc.returncode == 0:
                # `label in line` substring matching previously made
                # //:math match any line mentioning //:math_shared too (a
                # real label collision in this exact repo -- //:math and
                # //:math_shared are both configured), inflating
                # resolved_target_count with a target that was never
                # actually printed on its own line (CodeRabbit review,
                # PR #20). _extract_printed_labels() extracts the exact
                # `//...` token from each line instead.
                printed_labels = _extract_printed_labels(proc.stdout)
                resolved = [label for label in labels if label in printed_labels]
                evidence["targets"] = resolved
                if len(resolved) != len(labels):
                    evidence["note"] = (
                        f"cquery resolved {len(resolved)}/{len(labels)} configured targets"
                    )
            else:
                evidence["note"] = f"cquery --output=label_kind exited {proc.returncode}"
        except Exception as exc:  # pragma: no cover - defensive, evidence is best-effort
            evidence["note"] = f"cquery evidence unavailable: {exc}"
        return evidence

    def stage(self, build_result: BuildResult, dest_dir: Path) -> Dict[str, Any]:
        lib_dir = dest_dir / "lib"
        bin_dir = dest_dir / "bin"
        lib_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        manifest: Dict[str, Any] = {}
        for name, target in build_result.targets.items():
            if not target.built or target.path is None:
                manifest[name] = {"staged": False}
                continue
            out_subdir = bin_dir if target.kind == "executable" else lib_dir
            staged_name = self.profile.get("staged_names", {}).get(name, target.path.name)
            dest = out_subdir / staged_name
            shutil.copy2(target.path, dest)
            if target.kind in {"executable", "shared_library"}:
                # bazel-bin/ outputs are read-only (mode 0555 -- Bazel's own
                # convention for action outputs), and shutil.copy2 preserves
                # that mode on the staged copy. patchelf then needs to write
                # to it: this was invisible locally (root bypasses Unix
                # permission checks) but failed for real on GitHub Actions'
                # non-root runner user -- "patchelf --set-rpath failed" with
                # no further detail (real CI run, PR #19). Make this
                # profile's own staged copy writable first; never touches
                # bazel-bin's own read-only output.
                dest.chmod(0o755)
                # Bazel gives consumer_app a RUNPATH pointing at its own
                # generated _solib_*/ tree (relative to bazel-bin/, via
                # cc_import -- see consumer/BUILD.bazel), not at this
                # profile's canonical artifacts/lib/ layout. Once staged
                # (and especially once the original bazel-bin/ output tree
                # is gone, e.g. downloaded from a CI artifact elsewhere), the
                # dynamic linker can't resolve libmath.so at all -- unlike
                # the cmake/make profiles' $ORIGIN mistake, this isn't just
                # "wrong once relocated", it never worked outside the
                # original workspace (Codex review, PR #19). Rewrite the
                # *staged copy only* (never bazel-bin's own output) via
                # patchelf; never rewrite the original Bazel build tree.
                # No magic fallback: a missing patchelf or a failed rewrite
                # must not silently report this executable as successfully
                # staged with a RUNPATH that can't find libmath.so -- that
                # was exactly the previous version's gap (Codex review,
                # PR #19 follow-up). verify_environment() now requires
                # patchelf too, but stage() re-checks and fails closed
                # regardless of whether verify_environment() ran first.
                patchelf = shutil.which("patchelf")
                rewrite_failed = patchelf is None
                if patchelf:
                    staged_rpath = "$ORIGIN/../lib" if target.kind == "executable" else "$ORIGIN"
                    proc = self._run(
                        [patchelf, "--set-rpath", staged_rpath, str(dest)],
                        check=False,
                    )
                    rewrite_failed = proc.returncode != 0
                if rewrite_failed:
                    detail = "patchelf not found" if patchelf is None else "patchelf --set-rpath failed"
                    build_result.success = False
                    build_result.diagnostics.append(
                        f"could not rewrite RUNPATH for staged {target.kind} {name!r}: {detail}"
                    )
                    manifest[name] = {"staged": False, "error": detail}
                    continue
                # patchelf rewrites dest's bytes in place, so target's
                # sha256/size_bytes (computed from the pre-patch
                # bazel-bin file) would otherwise no longer match what's
                # actually staged -- build_result.targets[name] feeds
                # emit_build_output.py's targets_doc sha256/size_bytes
                # directly, not this method's own manifest dict, so
                # update it here (TargetResult isn't frozen) rather than
                # let build-output.json assert a digest for bytes that
                # aren't what's on disk.
                target = TargetResult.from_path(target.name, target.kind, dest)
                build_result.targets[name] = target
            manifest[name] = {
                "staged": True,
                "path": str(dest.relative_to(dest_dir)),
                "sha256": target.sha256,
                "size_bytes": target.size_bytes,
            }
        return manifest

    def describe(self) -> Dict[str, Any]:
        exe = self._bazel_executable()
        return {
            "backend": self.name,
            "bazel_version": self._tool_version(exe, "version"),
            "compiler": dict(self.profile.get("compiler", {})),
            "generator": None,
        }
