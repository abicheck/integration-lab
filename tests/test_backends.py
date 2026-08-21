"""Unit + smoke tests for ci/backends/.

- Unit tests (base.py dataclasses, get_backend_class()) run unconditionally.
- End-to-end smoke tests actually invoke bazel/cmake+ninja/make against
  ci/profiles.yaml's real profiles when the required executables are on
  PATH, and are skipped (not failed) otherwise -- this repo's CI/dev
  sandbox has all three installed (see this PR's own validation notes),
  but a minimal checkout without them should still collect a clean test
  run instead of failing on a missing tool this file doesn't own.
"""
from __future__ import annotations

import shutil

import pytest

from base import BuildBackend, TargetResult
from backends import build_backend, get_backend_class


def test_target_result_from_path_missing_file(tmp_path):
    result = TargetResult.from_path("x", "shared_library", tmp_path / "nope.so")
    assert result.built is False
    assert result.path is None
    assert result.sha256 is None


def test_target_result_from_path_existing_file(tmp_path):
    path = tmp_path / "libx.so"
    path.write_bytes(b"hello")
    result = TargetResult.from_path("x", "shared_library", path)
    assert result.built is True
    assert result.size_bytes == 5
    import hashlib

    assert result.sha256 == hashlib.sha256(b"hello").hexdigest()


def test_get_backend_class_resolves_all_three():
    from bazel import BazelBackend
    from cmake import CMakeBackend
    from make import MakeBackend

    assert get_backend_class("bazel") is BazelBackend
    assert get_backend_class("cmake") is CMakeBackend
    assert get_backend_class("make") is MakeBackend


def test_get_backend_class_rejects_unknown():
    with pytest.raises(ValueError):
        get_backend_class("nonexistent")


def _profiles_by_id():
    from select_profiles import load_profiles
    from pathlib import Path

    return load_profiles(Path(__file__).resolve().parent.parent / "ci" / "profiles.yaml")


REPO_ROOT = None


@pytest.fixture()
def repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("cmake") is None or shutil.which("ninja") is None, reason="cmake/ninja not installed")
@pytest.mark.skipif(shutil.which("g++-14") is None, reason="g++-14 not installed")
def test_cmake_backend_builds_real_targets(repo_root):
    profiles = _profiles_by_id()
    profile = profiles["linux-x86_64-gcc14-cxx17-cmake-ninja"]
    backend = build_backend(profile, repo_root)

    env = backend.verify_environment()
    assert env.ok, env.missing

    backend.clean()
    result = backend.build()
    assert result.success, result.diagnostics
    for name in profile["targets"]:
        assert result.targets[name].built, name


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.skipif(shutil.which("g++-14") is None, reason="g++-14 not installed")
def test_make_backend_builds_real_targets(repo_root):
    profiles = _profiles_by_id()
    profile = profiles["linux-x86_64-gcc14-cxx17-make-bear"]
    backend = build_backend(profile, repo_root)

    env = backend.verify_environment()
    assert env.ok, env.missing

    backend.clean()
    result = backend.build()
    assert result.success, result.diagnostics
    for name in profile["targets"]:
        assert result.targets[name].built, name


@pytest.mark.skipif(shutil.which("bazel") is None, reason="bazel not installed")
def test_bazel_backend_builds_real_targets(repo_root):
    profiles = _profiles_by_id()
    profile = profiles["linux-x86_64-gcc14-cxx17-bazel"]
    backend = build_backend(profile, repo_root)

    env = backend.verify_environment()
    assert env.ok, env.missing

    result = backend.build()
    assert result.success, result.diagnostics
    for name in profile["targets"]:
        assert result.targets[name].built, name
