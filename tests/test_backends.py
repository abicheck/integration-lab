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


@pytest.mark.parametrize(
    "contents",
    [
        "[]",
        "{}",
        "not json",
        "[null]",
        "[{}]",
        '[{"directory": "/tmp", "file": "src/math.cc"}]',
        '[{"directory": "/tmp", "command": "g++ -c src/math.cc"}]',
        '[{"file": "src/math.cc", "command": "g++ -c src/math.cc"}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "arguments": []}]',
        # Non-string path/command fields.  Each of these is a value a
        # scanner would later index as a path or re-parse as an argv, so
        # the compile database has to reject them as evidence rather than
        # forward them and crash somewhere with less context.
        '[{"directory": 7, "file": "src/math.cc", "command": "g++ -c a.cc"}]',
        '[{"directory": "/tmp", "file": ["src/math.cc"], "command": "g++ -c a.cc"}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "command": 7}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "command": "   "}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "arguments": "g++ -c a.cc"}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "arguments": ["g++", 7]}]',
        '[{"directory": "/tmp", "file": "src/math.cc", "arguments": [["g++"]]}]',
        '[{"directory": null, "file": null, "command": null}]',
        # A well-formed entry does not excuse a malformed sibling.
        '[{"directory": "/tmp", "file": "a.cc", "command": "g++ a.cc"}, 3]',
    ],
)
def test_make_evidence_requires_usable_database(tmp_path, monkeypatch, contents):
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    backend._build_dir.mkdir()
    (backend._build_dir / "compile_commands.json").write_text(contents)
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: "/usr/bin/bear")
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: None)

    with pytest.raises(BackendError, match="compile database"):
        backend.collect_evidence(result)


def test_make_evidence_rejects_malformed_utf8_database(tmp_path, monkeypatch):
    """A byte-corrupted capture is a BackendError, not a decode traceback."""
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    backend._build_dir.mkdir()
    (backend._build_dir / "compile_commands.json").write_bytes(
        b'[{"directory": "/tmp", "file": "\xff\xfe.cc", "command": "g++"}]'
    )
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: "/usr/bin/bear")
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: None)

    with pytest.raises(BackendError, match="not valid UTF-8"):
        backend.collect_evidence(result)


@pytest.mark.parametrize(
    "contents",
    [
        # `arguments` is the JSON Compilation Database's other legal form.
        '[{"directory": "/tmp", "file": "a.cc", "arguments": ["g++", "-c", "a.cc"]}]',
        # One usable invocation is enough when both keys are present.
        '[{"directory": "/tmp", "file": "a.cc", "command": "g++ -c a.cc",'
        ' "arguments": []}]',
    ],
)
def test_make_evidence_accepts_every_legal_invocation_form(
    tmp_path, monkeypatch, contents
):
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    backend._build_dir.mkdir()
    (backend._build_dir / "compile_commands.json").write_text(contents)
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: "/usr/bin/bear")
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: None)

    assert backend.collect_evidence(result)["compile_commands_present"] is True


def test_make_evidence_accepts_nonempty_command_array(tmp_path, monkeypatch):
    from make import MakeBackend

    backend = MakeBackend({"root": "."}, tmp_path)
    backend._build_dir.mkdir()
    database = backend._build_dir / "compile_commands.json"
    database.write_text(
        '[{"directory": "/tmp", "file": "src/math.cc", '
        '"command": "g++ -c src/math.cc"}]'
    )
    result = BuildResult(
        profile_id="make", backend="make", success=True, started_at=0, ended_at=1
    )
    monkeypatch.setattr("make.shutil.which", lambda tool: "/usr/bin/bear")
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: None)

    evidence = backend.collect_evidence(result)
    assert evidence["compile_commands_present"] is True
    assert evidence["compile_commands_path"] == str(database)


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


# --------------------------------------------------------------------------
# Bazel toolchain parity: every invocation must share the build's configuration
# --------------------------------------------------------------------------


def _bazel_backend(tmp_path):
    from bazel import BazelBackend

    return BazelBackend(
        {
            "id": "linux-x86_64-gcc14-cxx17-bazel",
            "root": ".",
            "targets": {"math": "//:math", "strings": "//strings_lib:strings"},
            "compiler": {"cc": "gcc-14", "cxx": "g++-14"},
        },
        tmp_path,
    )


def test_bazel_command_carries_the_toolchain(tmp_path):
    backend = _bazel_backend(tmp_path)
    argv = backend._bazel_command("cquery", "--output=files", "//:math")
    assert "--repo_env=CC=gcc-14" in argv
    assert "--repo_env=CXX=g++-14" in argv
    # Subcommand stays immediately after the executable; flags follow it.
    assert argv[1] == "cquery"
    assert argv[-2:] == ["--output=files", "//:math"]


def _captured_argvs(backend, monkeypatch):
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(backend, "_run", _fake_run)
    return calls


def test_cquery_evidence_runs_under_the_build_toolchain(tmp_path, monkeypatch):
    """A cquery without --repo_env analyses a different configured graph."""
    backend = _bazel_backend(tmp_path)
    calls = _captured_argvs(backend, monkeypatch)
    result = BuildResult(
        profile_id="bazel", backend="bazel", success=True, started_at=0, ended_at=1
    )
    evidence = backend.collect_evidence(result)
    assert calls, "cquery was never invoked"
    for argv in calls:
        assert "--repo_env=CC=gcc-14" in argv, argv
        assert "--repo_env=CXX=g++-14" in argv, argv
    # The receipt records the configuration it was collected under.
    assert evidence["toolchain_args"] == ["--repo_env=CC=gcc-14", "--repo_env=CXX=g++-14"]


def test_output_path_resolution_runs_under_the_build_toolchain(tmp_path, monkeypatch):
    backend = _bazel_backend(tmp_path)
    calls = _captured_argvs(backend, monkeypatch)
    with pytest.raises(BackendError):
        # No real bazel here, so resolution fails -- the argv is the subject.
        backend._resolve_output_path("//:math")
    assert calls and "--repo_env=CC=gcc-14" in calls[0]


def test_no_bazel_argv_is_assembled_outside_the_helper():
    """Guard against a new call site reintroducing the configuration split.

    `bazel build` and `bazel cquery` disagreeing on CC/CXX is invisible in a
    green run -- the binary is usually still the expected one -- so only a
    structural check catches it.
    """
    import inspect

    import bazel

    source = inspect.getsource(bazel.BazelBackend)
    # _bazel_executable() may only be referenced by the helper itself,
    # verify_environment(), and describe() (neither of which configures a
    # target graph).
    for line in source.splitlines():
        if "_bazel_executable()" not in line:
            continue
        assert (
            "return shutil.which" in line
            or "exe = self._bazel_executable()" in line
            or "return [self._bazel_executable(), subcommand" in line
        ), f"raw bazel argv assembled outside _bazel_command(): {line.strip()}"


# --------------------------------------------------------------------------
# CMake preset must come from the profile, never a hard-coded default
# --------------------------------------------------------------------------


def _cmake_backend(profile_overrides=None):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "ci"))
    from cmake import CMakeBackend
    from select_profiles import load_profiles

    repo_root = _Path(__file__).resolve().parent.parent
    profiles = load_profiles(repo_root / "ci" / "profiles.yaml")
    profile = dict(profiles["linux-x86_64-gcc14-cxx17-cmake-ninja"])
    profile.update(profile_overrides or {})
    return CMakeBackend(profile, repo_root)


def test_each_cmake_profile_resolves_its_own_preset_and_build_dir():
    """A hard-coded preset builds a Clang profile with GCC and mislabels it."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "ci"))
    from cmake import CMakeBackend
    from select_profiles import load_profiles

    repo_root = _Path(__file__).resolve().parent.parent
    profiles = load_profiles(repo_root / "ci" / "profiles.yaml")
    resolved = {}
    for profile_id, profile in profiles.items():
        if profile.get("backend") != "cmake":
            continue
        backend = CMakeBackend(profile, repo_root)
        resolved[profile_id] = (backend._preset, backend._build_dir)
    assert len(resolved) >= 2, "expected more than one cmake profile"
    presets = [preset for preset, _ in resolved.values()]
    build_dirs = [str(path) for _, path in resolved.values()]
    # Two profiles sharing a preset would build with one compiler; two
    # sharing a binaryDir would collect each other's objects.
    assert len(set(presets)) == len(presets), resolved
    assert len(set(build_dirs)) == len(build_dirs), resolved


def test_profile_without_a_preset_fails_closed():
    backend = _cmake_backend()
    del backend.profile["cmake_preset"]
    with pytest.raises(BackendError, match="declares no cmake_preset"):
        _ = backend._build_dir


def test_unknown_preset_fails_closed():
    backend = _cmake_backend({"cmake_preset": "does-not-exist"})
    with pytest.raises(BackendError, match="no configure preset named"):
        _ = backend._build_dir


def test_build_dir_comes_from_the_presets_file():
    """Read, not assumed, so preset and output directory cannot diverge."""
    backend = _cmake_backend({"cmake_preset": "clang18-cxx17"})
    assert backend._build_dir.name == "build-clang18"
    assert _cmake_backend()._build_dir.name == "build"
