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

from base import BackendError, BuildBackend, BuildResult, TargetResult
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


def test_make_environment_requires_bear(tmp_path, monkeypatch):
    from make import MakeBackend

    backend = MakeBackend(
        {"root": ".", "compiler": {"cxx": "g++-14"}}, tmp_path
    )
    monkeypatch.setattr(
        backend,
        "_tool_version",
        lambda tool, version_flag="--version": None if tool == "bear" else "version",
    )

    check = backend.verify_environment()

    assert not check.ok
    assert check.missing == ["bear"]


def test_make_evidence_fails_when_bear_disappears(tmp_path, monkeypatch):
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: None)

    with pytest.raises(BackendError, match="required by the Make contract"):
        backend.collect_evidence(result)


def test_make_evidence_requires_generated_database(tmp_path, monkeypatch):
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: "/usr/bin/bear")
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: None)

    with pytest.raises(BackendError, match="without producing"):
        backend.collect_evidence(result)


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
@pytest.mark.skipif(shutil.which("bear") is None, reason="bear not installed")
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


def test_extract_printed_labels_no_substring_collision():
    # Real `bazel cquery --output=label_kind` output for a query resolving
    # both //:math and //:math_shared (captured from a real run against
    # this repo's own BUILD.bazel) -- //:math is a substring of
    # //:math_shared, so naive `label in line` matching would previously
    # report //:math "resolved" even from a query where only
    # //:math_shared was ever configured/printed.
    from bazel import _extract_printed_labels

    real_output = "\n".join([
        "Computing main repo mapping: ",
        "Loading: ",
        "Loading: 0 packages loaded",
        "Analyzing: 2 targets (0 packages loaded, 0 targets configured)",
        "INFO: Analyzed 2 targets (0 packages loaded, 0 targets configured).",
        "INFO: Found 2 targets...",
        "cc_binary rule //:math (9bbc831)",
        "cc_shared_library rule //:math_shared (9bbc831)",
        "INFO: Elapsed time: 0.175s, Critical Path: 0.00s",
        "INFO: 0 processes.",
        "INFO: Build completed successfully, 0 total actions",
    ])
    assert _extract_printed_labels(real_output) == {"//:math", "//:math_shared"}


def test_extract_printed_labels_no_false_positive_for_unprinted_label():
    # Only //:math_shared is actually printed (e.g. //:math failed to
    # resolve) -- //:math must NOT be reported as resolved just because
    # it's a substring of a line that IS present.
    from bazel import _extract_printed_labels

    output = "cc_shared_library rule //:math_shared (9bbc831)\n"
    printed = _extract_printed_labels(output)
    assert printed == {"//:math_shared"}
    assert "//:math" not in printed
