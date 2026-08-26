"""CMake+Ninja backend: drives buildsystems/cmake/ (CMakeLists.txt +
CMakePresets.json) via real `cmake --preset ...` / `cmake --build ...`
subprocess calls.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from base import BackendError, BuildBackend, BuildResult, EnvironmentCheck, TargetResult

_EXECUTABLE_TARGETS = {"consumer"}
_PRESETS_FILE = "CMakePresets.json"


class CMakeBackend(BuildBackend):
    name = "cmake"

    @property
    def _preset(self) -> str:
        """The CMake preset this profile declares.

        Previously hard-coded to "gcc14-cxx17" for every cmake profile.
        That was invisible while there was only one, but it means a profile
        declaring a different producer builds with the WRONG compiler while
        build-output.json still reports the declared one -- corrupted
        producer provenance rather than an honest failure (Codex review,
        PR #30, on the clang-18 profile this PR adds).

        Fails closed when unset: silently falling back to another profile's
        preset is exactly the bug.
        """
        preset = self.profile.get("cmake_preset")
        if not preset:
            raise BackendError(
                f"profile {self.profile.get('id')!r} declares no cmake_preset; "
                "refusing to guess one, since guessing means building with a "
                "different compiler than the profile claims"
            )
        return preset

    @property
    def _build_dir(self) -> Path:
        """The preset's own binaryDir, read from CMakePresets.json.

        Read rather than assumed so the preset and the directory the
        backend collects outputs from cannot diverge: two presets that
        share one binaryDir would have each other's objects, and a preset
        whose binaryDir moved would silently stage a stale tree.
        """
        presets_path = self.root / _PRESETS_FILE
        try:
            document = json.loads(presets_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(f"cannot read {presets_path}: {exc}") from exc
        for entry in document.get("configurePresets", []):
            if entry.get("name") != self._preset:
                continue
            binary_dir = entry.get("binaryDir")
            if not binary_dir:
                raise BackendError(
                    f"{presets_path}: preset {self._preset!r} declares no binaryDir"
                )
            # Only ${sourceDir} is substituted; anything else is a macro
            # this backend has not been taught and must not silently mangle.
            resolved = binary_dir.replace("${sourceDir}", str(self.root))
            if "${" in resolved:
                raise BackendError(
                    f"{presets_path}: preset {self._preset!r} binaryDir "
                    f"{binary_dir!r} uses a macro this backend cannot resolve"
                )
            return Path(resolved)
        raise BackendError(
            f"{presets_path}: no configure preset named {self._preset!r}"
        )

    def verify_environment(self) -> EnvironmentCheck:
        missing = []
        versions = {}
        for tool in ("cmake", self.profile.get("generator", "Ninja") or "Ninja"):
            exe = tool.lower() if tool == "Ninja" else tool
            version = self._tool_version(exe)
            if version is None:
                missing.append(exe)
            else:
                versions[exe] = version
        compiler = self.profile.get("compiler", {})
        cxx = compiler.get("cxx", "c++")
        cxx_version = self._tool_version(cxx)
        if cxx_version is None:
            missing.append(cxx)
        else:
            versions[cxx] = cxx_version
        return EnvironmentCheck(ok=not missing, tool_versions=versions, missing=missing)

    def clean(self) -> None:
        if self._build_dir.exists():
            shutil.rmtree(self._build_dir)

    def configure(self) -> str:
        proc = self._run(["cmake", "--preset", self._preset])
        return proc.stdout

    def build(self) -> BuildResult:
        started = time.time()
        diagnostics = []
        success = True
        configure_log = ""
        build_log = ""
        try:
            configure_log = self.configure()
            proc = self._run(["cmake", "--build", "--preset", self._preset])
            build_log = proc.stdout
        except BackendError as exc:
            build_log = str(exc)
            success = False
            diagnostics.append(f"cmake build failed: {exc}")

        targets: Dict[str, TargetResult] = {}
        for name, cmake_target in dict(self.profile["targets"]).items():
            kind = "executable" if name in _EXECUTABLE_TARGETS else "shared_library"
            path = self._resolve_output_path(cmake_target, kind) if success else None
            result = TargetResult.from_path(name, kind, path)
            if not result.built:
                success = False
                diagnostics.append(f"target {name!r} ({cmake_target}) produced no output")
            targets[name] = result

        return BuildResult(
            profile_id=self.profile["id"],
            backend=self.name,
            success=success,
            started_at=started,
            ended_at=time.time(),
            targets=targets,
            diagnostics=diagnostics,
            configure_log=configure_log,
            build_log=build_log,
        )

    def _resolve_output_path(self, cmake_target: str, kind: str) -> Path:
        if kind == "executable":
            candidate = self._build_dir / cmake_target
            return candidate
        # This candidate path is always "lib<target>.so" -- the canonical,
        # unversioned filename matching the Bazel/Make backends' staged
        # filename (see BuildBackend design note: canonical candidate
        # paths must be identical across build systems, e.g.
        # artifacts/lib/libmath.so). What that path actually IS on disk
        # depends on whether math/strings set a SOVERSION/VERSION: neither
        # is set (their current CMakeLists.txt shape -- see that file's own
        # comment), so CMake's own default SONAME behavior applies (SONAME
        # == OUTPUT_NAME, verified against a real build with `readelf -d`)
        # and this path is a plain regular file, not a dev symlink. If
        # SOVERSION/VERSION were introduced instead, CMake would make this
        # path a symlink to a versioned real file
        # (lib<target>.so.<SOVERSION>.<...>) -- TargetResult.from_path()/
        # stage() read file bytes (sha256/copy2) by following a symlink
        # either way, so this path stays correct in both shapes; only
        # stage()'s own SONAME-companion-staging block (see its own
        # comment) would start doing real work again instead of its
        # current no-op.
        return self._build_dir / f"lib{cmake_target}.so"

    def collect_evidence(self, build_result: BuildResult) -> Dict[str, Any]:
        generator = self.profile.get("generator", "Ninja") or "Ninja"
        evidence: Dict[str, Any] = {"kind": "compile_commands", "generator": generator}
        compile_commands = self._build_dir / "compile_commands.json"
        if compile_commands.is_file():
            evidence["compile_commands_path"] = str(compile_commands)
            evidence["compile_commands_present"] = True
        else:
            evidence["compile_commands_present"] = False
            evidence["note"] = "compile_commands.json not found under build/"
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
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, dest)
            companion = self.profile.get("soname_companions", {}).get(name)
            if companion:
                shutil.copy2(target.path, dest.parent / companion)
            if target.kind == "shared_library":
                # CMakeLists.txt sets no SOVERSION/VERSION for math/strings
                # (see that file's own comment: CMake's default SONAME ==
                # OUTPUT_NAME is used instead, matching Bazel's/Make's own
                # identical -Wl,-soname), so target.path is a plain regular
                # file, not a dev symlink, and this block is a no-op
                # (readlink() raises OSError -> soname=None -> nothing extra
                # staged). Kept, rather than removed outright, for the
                # general case: a future CMakeLists.txt change introducing
                # SOVERSION/VERSION would make target.path a "lib<name>.so"
                # symlink again, and consumer_app would then need the
                # SONAME-named ("lib<name>.so.<SOVERSION>") companion file
                # staged alongside it too, exactly as this block already
                # handles (Codex review, PR #19, historical: this omission
                # is what originally made a staged-only consumer_app unable
                # to resolve its dependency at runtime).
                try:
                    soname = target.path.readlink().name
                except OSError:
                    soname = None
                if soname and soname != dest.name:
                    shutil.copy2(target.path, out_subdir / soname)
            manifest[name] = {
                "staged": True,
                "path": str(dest.relative_to(dest_dir)),
                "sha256": target.sha256,
                "size_bytes": target.size_bytes,
            }
        return manifest

    def describe(self) -> Dict[str, Any]:
        compiler = self.profile.get("compiler", {})
        return {
            "backend": self.name,
            "cmake_version": self._tool_version("cmake"),
            "generator": self.profile.get("generator", "Ninja"),
            "compiler": dict(compiler),
            "cxx_version": self._tool_version(compiler.get("cxx", "c++")),
        }
