"""Unit tests for ci/emit_build_output.py -- staging a BuildResult into the
canonical abicheck-build-<profile-id>/ directory and its build-output.json
document. Uses a small fake backend (duck-typed against the same methods
ci.backends.base.BuildBackend declares) rather than a real bazel/cmake/make
toolchain, so this stays fast and deterministic -- the real backends
themselves are exercised by tests/test_backends_*.py and by this PR's own
documented manual validation (real `bazel build` / `cmake --build` /
`make` runs).
"""
from __future__ import annotations

import json

import pytest

from base import BuildResult, TargetResult
from emit_build_output import _compiler_abi_macros, stage_profile
from validate_build_output import validate_document, validate_file


class _FakeBackend:
    """Duck-typed BuildBackend: implements exactly the methods
    stage_profile() calls (stage/collect_evidence/describe/_tool_version),
    nothing else -- no subprocess calls anywhere.
    """

    def __init__(self, stage_manifest, evidence, description):
        self._stage_manifest = stage_manifest
        self._evidence = evidence
        self._description = description

    def stage(self, build_result, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        return self._stage_manifest

    def collect_evidence(self, build_result):
        return dict(self._evidence)

    def describe(self):
        return dict(self._description)

    def _tool_version(self, exe):
        return f"{exe} (fake)" if exe else None


def test_compiler_abi_macros_records_gcc_abi_version(monkeypatch):
    class Result:
        stdout = "\n".join([
            "#define __GXX_ABI_VERSION 1019",
            "#define _GLIBCXX_USE_CXX11_ABI 1",
            "#define __cplusplus 201703L",
            "#define __IGNORED 1",
        ])

    monkeypatch.setattr("emit_build_output.subprocess.run", lambda *args, **kwargs: Result())

    macros = _compiler_abi_macros("g++-14")

    assert "__GXX_ABI_VERSION 1019" in macros
    assert "__IGNORED" not in macros


def _make_target(tmp_path, name, kind, content=b"fake-binary-bytes"):
    path = tmp_path / f"{name}.bin"
    path.write_bytes(content)
    return TargetResult.from_path(name, kind, path)


@pytest.fixture()
def profile():
    return {
        "id": "test-profile",
        "backend": "cmake",
        "contract": False,
        "root": "buildsystems/cmake",
        "generator": "Ninja",
        "compiler": {"family": "gcc", "cc": "gcc-14", "cxx": "g++-14", "standard": "c++17"},
        "targets": {"math": "math", "consumer": "consumer_app"},
        "header_roots": ["include"],
        "target_header_roots": {"math": ["include"], "consumer": []},
    }


def test_stage_profile_writes_expected_layout(tmp_path, profile):
    repo_root = tmp_path / "repo"
    (repo_root / "include" / "abicheck_lab").mkdir(parents=True)
    (repo_root / "include" / "abicheck_lab" / "math.h").write_text("// header\n")

    math_target = _make_target(tmp_path, "math", "shared_library")
    consumer_target = _make_target(tmp_path, "consumer", "executable")
    build_result = BuildResult(
        profile_id=profile["id"],
        backend="cmake",
        success=True,
        started_at=0.0,
        ended_at=1.5,
        targets={"math": math_target, "consumer": consumer_target},
        diagnostics=[],
        configure_log="",
        build_log="",
    )

    backend = _FakeBackend(
        stage_manifest={
            "math": {"staged": True, "path": "lib/math.bin", "sha256": math_target.sha256, "size_bytes": math_target.size_bytes},
            "consumer": {"staged": True, "path": "bin/consumer.bin", "sha256": consumer_target.sha256, "size_bytes": consumer_target.size_bytes},
        },
        evidence={"kind": "fake", "compile_commands_present": False},
        description={"backend": "cmake", "generator": "Ninja"},
    )

    out_dir = tmp_path / "abicheck-build-test-profile"
    doc = stage_profile(profile, repo_root, build_result, backend, out_dir)

    assert (out_dir / "build-output.json").is_file()
    assert (out_dir / "provenance" / "build-system.json").is_file()
    assert (out_dir / "headers" / "include" / "abicheck_lab" / "math.h").is_file()

    assert (out_dir / "lab-build-output.json").is_file()
    assert doc["profile"]["id"] == "test-profile"
    assert json.loads((out_dir / "lab-build-output.json").read_text())["profile"]["contract"] is False
    assert next(t for t in doc["targets"] if t["id"] == "math")["binary"] == "artifacts/lib/math.bin"
    assert next(t for t in doc["targets"] if t["id"] == "math")["kind"] == "shared_library"
    assert next(t for t in doc["targets"] if t["id"] == "consumer")["kind"] == "executable"
    assert doc["digests"]["artifacts/lib/math.bin"] == f"sha256:{math_target.sha256}"
    assert any(t["id"] == "consumer" for t in doc["targets"])
    assert next(t for t in doc["targets"] if t["id"] == "math")["public_header_roots"] == ["headers/include"]
    assert doc["schema"] == "abicheck.build-output/v1"

    on_disk = json.loads((out_dir / "build-output.json").read_text())
    assert on_disk == doc


def test_stage_profile_reflects_missing_target(tmp_path, profile):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    build_result = BuildResult(
        profile_id=profile["id"],
        backend="cmake",
        success=False,
        started_at=0.0,
        ended_at=0.5,
        targets={"math": TargetResult(name="math", kind="shared_library", path=None, built=False)},
        diagnostics=["target 'math' produced no output"],
    )
    backend = _FakeBackend(
        stage_manifest={"math": {"staged": False}},
        evidence={"kind": "fake"},
        description={"backend": "cmake"},
    )

    out_dir = tmp_path / "out"
    doc = stage_profile(profile, repo_root, build_result, backend, out_dir)

    legacy = json.loads((out_dir / "lab-build-output.json").read_text())
    assert legacy["success"] is False
    assert legacy["targets"]["math"]["built"] is False
    assert legacy["targets"]["math"]["path"] is None
    assert legacy["diagnostics"] == [
        "target 'math' produced no output",
        f"header_roots entry 'include' does not exist under {repo_root}",
    ]


def test_stage_profile_marks_built_but_unstaged_target_skipped(tmp_path, profile):
    repo_root = tmp_path / "repo"
    (repo_root / "include").mkdir(parents=True)
    target = _make_target(tmp_path, "math", "shared_library")
    build_result = BuildResult(
        profile_id=profile["id"], backend="cmake", success=True,
        started_at=0.0, ended_at=0.1, targets={"math": target}, diagnostics=[],
    )
    backend = _FakeBackend(
        stage_manifest={"math": {"staged": False}},
        evidence={"kind": "fake"}, description={"backend": "cmake"},
    )

    doc = stage_profile(profile, repo_root, build_result, backend, tmp_path / "out")

    assert doc["targets"] == []
    assert doc["diagnostics"]["skipped_targets"] == ["math"]


def test_staged_build_output_validates_against_schema(tmp_path, profile):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = _make_target(tmp_path, "math", "shared_library")
    build_result = BuildResult(
        profile_id=profile["id"],
        backend="cmake",
        success=True,
        started_at=0.0,
        ended_at=0.1,
        targets={"math": target, "consumer": TargetResult(name="consumer", kind="executable", path=None, built=False)},
        diagnostics=["target 'consumer' produced no output"],
    )
    build_result.success = False
    backend = _FakeBackend(
        stage_manifest={"math": {"staged": True, "path": "lib/math.bin"}, "consumer": {"staged": False}},
        evidence={"kind": "fake"},
        description={"backend": "cmake"},
    )
    out_dir = tmp_path / "abicheck-build-test-profile"
    stage_profile(profile, repo_root, build_result, backend, out_dir)

    errors = validate_file(out_dir / "build-output.json")
    assert errors == []


def test_validate_document_flags_missing_required_field():
    doc = {"schema": "abicheck.build-output/v1"}
    errors = validate_document(doc)
    assert errors  # missing every other required top-level field


def test_validate_document_flags_bad_schema():
    errors = validate_document({"schema": "wrong"})
    assert errors
