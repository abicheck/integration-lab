"""CMake+Ninja backend: drives buildsystems/cmake/ (CMakeLists.txt +
CMakePresets.json) via real `cmake --preset ...` / `cmake --build ...`
subprocess calls.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict

from base import BackendError, BuildBackend, BuildResult, EnvironmentCheck, TargetResult

_EXECUTABLE_TARGETS = {"consumer"}
_PRESET = "gcc14-cxx17"


class CMakeBackend(BuildBackend):
    name = "cmake"

    @property
    def _build_dir(self) -> Path:
        return self.root / "build"

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
        proc = self._run(["cmake", "--preset", _PRESET])
        return proc.stdout

    def build(self) -> BuildResult:
        started = time.time()
        diagnostics = []
        success = True
        configure_log = ""
        build_log = ""
        try:
            configure_log = self.configure()
            proc = self._run(["cmake", "--build", "--preset", _PRESET])
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
        # Shared libraries are versioned (lib<target>.so.1.0.0 with a
        # lib<target>.so symlink). Return the symlink path itself, not its
        # resolved target: TargetResult.from_path()/stage() read file bytes
        # (sha256/copy2) by following it, which yields the real content, but
        # keep the *name* the canonical, unversioned "lib<target>.so" --
        # matching the Bazel/Make backends' staged filename (see BuildBackend
        # design note: canonical candidate paths must be identical across
        # build systems, e.g. artifacts/lib/libmath.so). Resolving the
        # symlink here previously staged the versioned real filename
        # (libmath.so.1.0.0) instead, diverging from bazel.py/make.py.
        symlink = self._build_dir / f"lib{cmake_target}.so"
        return symlink

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
            dest = out_subdir / target.path.name
            shutil.copy2(target.path, dest)
            if target.kind == "shared_library":
                # target.path is the bare "lib<name>.so" dev symlink (see
                # _resolve_output_path); consumer_app is linked against the
                # *SONAME* ("lib<name>.so.<SOVERSION>", e.g. libmath.so.1 --
                # SOVERSION 1 in CMakeLists.txt), not the bare name, so the
                # staged directory needs that file too or a staged-only
                # consumer_app can't resolve its dependency at runtime
                # (Codex review, PR #19). CMake's own symlink chain already
                # names it -- readlink the bare symlink's immediate target
                # (one hop: "lib<name>.so" -> "lib<name>.so.<SOVERSION>")
                # rather than re-deriving SOVERSION from CMakeLists.txt.
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
