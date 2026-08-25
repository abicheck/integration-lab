"""Make backend with mandatory Bear compile evidence.

The native Make profile is a project contract.  It must therefore fail
closed when it cannot produce the compile database consumed by the scanner;
a successful binary build is not sufficient evidence for that profile.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict

from base import BackendError, BuildBackend, BuildResult, EnvironmentCheck, TargetResult

_EXECUTABLE_TARGETS = {"consumer"}


class MakeBackend(BuildBackend):
    name = "make"

    @property
    def _build_dir(self) -> Path:
        return self.root / "build"

    def verify_environment(self) -> EnvironmentCheck:
        missing = []
        versions = {}
        for tool in ("make", "bear"):
            version = self._tool_version(tool)
            if version is None:
                missing.append(tool)
            else:
                versions[tool] = version
        compiler = self.profile.get("compiler", {})
        cxx = compiler.get("cxx", "c++")
        cxx_version = self._tool_version(cxx)
        if cxx_version is None:
            missing.append(cxx)
        else:
            versions[cxx] = cxx_version
        return EnvironmentCheck(ok=not missing, tool_versions=versions, missing=missing)

    def clean(self) -> None:
        # check=False previously discarded a failed `make clean` (e.g. a
        # permission/ownership error on a stale build/ tree from a prior
        # run) -- build() would then silently reuse/consider-up-to-date
        # the old outputs instead of failing, staging stale binaries
        # rather than the current source (Codex review, PR #19). _run()'s
        # default check=True raises BackendError instead.
        self._run(["make", "clean"])

    def configure(self) -> str:
        return ""  # Make has no separate configure step.

    def _cc_cxx_args(self) -> list:
        compiler = self.profile.get("compiler", {})
        args = []
        if compiler.get("cc"):
            args.append(f"CC={compiler['cc']}")
        if compiler.get("cxx"):
            args.append(f"CXX={compiler['cxx']}")
        return args

    def build(self) -> BuildResult:
        started = time.time()
        diagnostics = []
        success = True
        build_log = ""
        try:
            proc = self._run(["make", "all", *self._cc_cxx_args()])
            build_log = proc.stdout
        except BackendError as exc:
            build_log = str(exc)
            success = False
            diagnostics.append(f"make build failed: {exc}")

        targets: Dict[str, TargetResult] = {}
        for name, filename in dict(self.profile["targets"]).items():
            kind = "executable" if name in _EXECUTABLE_TARGETS else "shared_library"
            path = (self._build_dir / filename) if success else None
            result = TargetResult.from_path(name, kind, path)
            if not result.built:
                success = False
                diagnostics.append(f"target {name!r} ({filename}) produced no output")
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

    def collect_evidence(self, build_result: BuildResult) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {"kind": "make+bear"}
        if shutil.which("bear") is None:
            raise BackendError(
                "bear is required by the Make contract profile but was not found on PATH"
            )
        self._run(["make", "compiledb", *self._cc_cxx_args()])
        compile_commands = self._build_dir / "compile_commands.json"
        if not compile_commands.is_file():
            raise BackendError(
                f"Make evidence collection succeeded without producing {compile_commands}"
            )
        try:
            commands = json.loads(compile_commands.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError(f"Make compile database is not valid JSON: {exc}") from exc
        if not isinstance(commands, list) or not commands:
            raise BackendError(
                "Make compile database must be a non-empty JSON array of compile commands"
            )
        if any(not isinstance(command, dict) for command in commands):
            raise BackendError("Make compile database entries must be JSON objects")
        evidence["compile_commands_present"] = True
        evidence["compile_commands_path"] = str(compile_commands)
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
        compiler = self.profile.get("compiler", {})
        return {
            "backend": self.name,
            "make_version": self._tool_version("make"),
            "generator": None,
            "compiler": dict(compiler),
            "cxx_version": self._tool_version(compiler.get("cxx", "c++")),
            "bear_available": shutil.which("bear") is not None,
        }
