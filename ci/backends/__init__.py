"""Build-backend abstraction (PR1 of the multi-build-system integration
plan -- see UPSTREAM_TO_ABICHECK.md and ci/profiles.yaml's own header
comment).

Each module here (bazel.py / cmake.py / make.py) implements the
BuildBackend ABC declared in base.py against a real toolchain, actually
invoking the corresponding subprocess (`bazel`, `cmake`+`ninja`, `make`).
Nothing in this package is GitHub-Actions specific: no `GITHUB_*` env var
is read, no `::group::`/`::set-output::` workflow-command is written, no
artifact upload happens here -- that keeps every backend runnable and
testable from a plain `pytest` invocation, and reusable from any future
driver (a different CI system, a local dev script) without change.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Type

# Modules in this package import each other as plain top-level modules
# (`from base import ...`), matching the run-as-a-script pattern
# scripts/*.py and tests/conftest.py already use elsewhere in this repo --
# so this package's own directory needs to be on sys.path too.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from base import BuildBackend  # noqa: E402


def get_backend_class(name: str) -> Type[BuildBackend]:
    """Resolve a profile's `backend` field to its BuildBackend subclass."""
    if name == "bazel":
        from bazel import BazelBackend

        return BazelBackend
    if name == "cmake":
        from cmake import CMakeBackend

        return CMakeBackend
    if name == "make":
        from make import MakeBackend

        return MakeBackend
    raise ValueError(f"unknown backend: {name!r}")


def build_backend(profile: Dict[str, Any], repo_root: Path) -> BuildBackend:
    return get_backend_class(profile["backend"])(profile, repo_root)
