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
        # This candidate path is always "lib<target>.so" -- the canonical,
        # unversioned filename matching the Bazel/Make backends' staged
        # filename (see BuildBackend design note: canonical candidate
        # paths must be identical across build systems, e.g.
        # artifacts/lib/libmath.so). What that path actually IS on disk
        # depends on whether math/strings set a SOVERSION/VERSION: with
        # NO_SONAME TRUE and neither set (their current CMakeLists.txt
        # shape -- see that file's own comment), it's a plain regular
        # file. If SOVERSION/VERSION were reintroduced, CMake would instead
        # make this path a symlink to a versioned real file
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
            dest = out_subdir / target.path.name
            shutil.copy2(target.path, dest)
            if target.kind == "shared_library":
                # CMakeLists.txt now sets NO_SONAME TRUE for math/strings
                # (aligning with Bazel's own no-SONAME `math` shape -- see
                # that file's own comment for why), so target.path is a
                # plain regular file, not a dev symlink, and this block is
                # a no-op (readlink() raises OSError -> soname=None ->
                # nothing extra staged). Kept, rather than removed
                # outright, for the general case: a future CMakeLists.txt
                # change reintroducing SOVERSION/VERSION would make
                # target.path a "lib<name>.so" symlink again, and
                # consumer_app would then need the SONAME-named
                # ("lib<name>.so.<SOVERSION>") companion file staged
                # alongside it too, exactly as this block already handles
                # (Codex review, PR #19, historical: this omission is what
                # originally made a staged-only consumer_app unable to
                # resolve its dependency at runtime).
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
