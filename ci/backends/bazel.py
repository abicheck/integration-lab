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


class BazelBackend(BuildBackend):
    name = "bazel"

    def _bazel_executable(self) -> str:
        return shutil.which("bazel") or "bazel"

    def verify_environment(self) -> EnvironmentCheck:
        exe = self._bazel_executable()
        version = self._tool_version(exe, "version")
        if version is None:
            return EnvironmentCheck(ok=False, missing=[exe], notes=["bazel not found on PATH"])
        return EnvironmentCheck(ok=True, tool_versions={"bazel": version})

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

    def build(self) -> BuildResult:
        started = time.time()
        labels = self._labels()
        diagnostics = []
        build_log = ""
        success = True
        try:
            proc = self._run([self._bazel_executable(), "build", *labels.values()])
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
        evidence: Dict[str, Any] = {"kind": "bazel-cquery", "targets": labels}
        try:
            proc = self._run(
                [self._bazel_executable(), "cquery", "--output=label_kind", *labels],
                check=False,
            )
            evidence["label_kind"] = proc.stdout.strip()
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
            dest = out_subdir / target.path.name
            shutil.copy2(target.path, dest)
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
