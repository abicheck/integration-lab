"""BuildBackend abstract base class plus the small result dataclasses every
concrete backend (bazel.py / cmake.py / make.py) returns.

Design goals, all deliberate:

- No GitHub-Actions-specific code anywhere in this file or its
  implementations -- a backend is a plain Python object driven by
  subprocess calls, so `pytest` can exercise it directly and a future
  driver (a different CI system, a local script) can reuse it unchanged.
- Every method actually shells out to the real toolchain (`bazel`,
  `cmake`/`ninja`, `make`/`bear`) -- nothing here is a mock or a stub that
  only pretends to build. verify_environment() is the one method allowed
  to *report* an unavailable tool rather than crash, so a driver can
  degrade a profile to "skipped: toolchain missing" instead of a hard
  failure when e.g. `bear` isn't installed (see make.py).
"""
from __future__ import annotations

import dataclasses
import hashlib
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional


class BackendError(RuntimeError):
    """Raised when a build step fails in a way the caller must not ignore."""


@dataclasses.dataclass
class EnvironmentCheck:
    """Result of verify_environment(): whether every executable this
    backend needs is actually on PATH and runnable, with enough detail to
    explain a failure without re-deriving it.
    """

    ok: bool
    tool_versions: Dict[str, str] = dataclasses.field(default_factory=dict)
    missing: List[str] = dataclasses.field(default_factory=list)
    notes: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class TargetResult:
    """One built artifact (a shared library or an executable)."""

    name: str
    kind: str  # "shared_library" | "executable"
    path: Optional[Path]  # absolute path to the built artifact, or None if not built
    built: bool
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None

    @staticmethod
    def from_path(name: str, kind: str, path: Optional[Path]) -> "TargetResult":
        if path is None or not path.is_file():
            return TargetResult(name=name, kind=kind, path=None, built=False)
        data = path.read_bytes()
        return TargetResult(
            name=name,
            kind=kind,
            path=path,
            built=True,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


@dataclasses.dataclass
class BuildResult:
    """Everything emit_build_output.py needs to stage canonical output for
    one profile run, produced by BuildBackend.build().
    """

    profile_id: str
    backend: str
    success: bool
    started_at: float
    ended_at: float
    targets: Dict[str, TargetResult] = dataclasses.field(default_factory=dict)
    diagnostics: List[str] = dataclasses.field(default_factory=list)
    configure_log: str = ""
    build_log: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


class BuildBackend(ABC):
    """One concrete build-system driver for one profile entry from
    ci/profiles.yaml. Instantiated per (profile, repo_root) pair.
    """

    #: Overridden by each subclass to name the executable(s) it needs.
    name: str = "base"

    def __init__(self, profile: Dict[str, Any], repo_root: Path):
        self.profile = profile
        self.repo_root = Path(repo_root).resolve()
        self.root = (self.repo_root / profile["root"]).resolve()

    # -- helpers shared by every backend -----------------------------------

    def _run(
        self,
        argv: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Actually invoke a subprocess (never mocked) and capture its
        combined stdout/stderr as text, for inclusion in configure_log /
        build_log / diagnostics.
        """
        proc = subprocess.run(
            argv,
            cwd=str(cwd or self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if check and proc.returncode != 0:
            raise BackendError(
                f"command failed ({proc.returncode}): {shlex.join(argv)}\n{proc.stdout}"
            )
        return proc

    def _tool_version(self, executable: str, version_flag: str = "--version") -> Optional[str]:
        try:
            proc = subprocess.run(
                [executable, version_flag],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            return None
        if proc.returncode != 0 and not proc.stdout:
            return None
        return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else None

    # -- the abstract contract ----------------------------------------------

    @abstractmethod
    def verify_environment(self) -> EnvironmentCheck:
        """Check that every executable this backend needs is present and
        runnable, WITHOUT building anything.
        """

    @abstractmethod
    def clean(self) -> None:
        """Remove any prior build output for this profile's root, so build()
        below runs against a clean tree."""

    @abstractmethod
    def configure(self) -> str:
        """Run the configure/generate step (cmake generate, or a no-op for
        bazel/make). Returns the captured log text."""

    @abstractmethod
    def build(self) -> BuildResult:
        """Actually build every target this profile declares. Calls
        configure() itself if this backend needs to. Returns a populated
        BuildResult -- success reflects whether every declared target was
        produced, not just whether the build command exited 0."""

    @abstractmethod
    def collect_evidence(self, build_result: BuildResult) -> Dict[str, Any]:
        """Gather backend-specific build evidence (e.g. a
        compile_commands.json path, a Bazel query, resolved compiler
        version strings) as a small, JSON-serializable dict. Never raises
        for missing *optional* evidence (e.g. no compile_commands.json) --
        that shows up as a note, not an exception."""

    @abstractmethod
    def stage(self, build_result: BuildResult, dest_dir: Path) -> Dict[str, Any]:
        """Copy this profile's built artifacts into dest_dir/{lib,bin} and
        return a manifest describing what was staged. dest_dir is created
        if missing."""

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """Return this backend's resolved compiler/toolchain identity
        (family, executable, --version output, standard) -- used for the
        provenance/build-system.json this profile's staged output emits.
        """
