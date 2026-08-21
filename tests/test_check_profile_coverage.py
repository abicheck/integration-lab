"""Unit tests for ci/check_profile_coverage.py -- the profile-aware
coverage contract (PR2 item 4). Uses fabricated staged directories (no real
build needed -- this script only reads build-output.json/report JSON/staged
files, never shells out itself) for a fast, deterministic test.
"""
from __future__ import annotations

import hashlib
import json

from check_profile_coverage import evaluate

_BAZEL_PROFILE = {
    "id": "p-bazel",
    "backend": "bazel",
    "coverage": {"checked_targets": ["math"], "require_public_headers": True, "min_resolved_targets": 1},
}

_CMAKE_PROFILE = {
    "id": "p-cmake",
    "backend": "cmake",
    "coverage": {"checked_targets": ["math"], "require_public_headers": True, "min_compile_units": 1},
}


def _stage(tmp_path, *, backend_evidence, sha256, header_files=("include/api.h",)):
    staged = tmp_path / "staged"
    lib_dir = staged / "artifacts" / "lib"
    lib_dir.mkdir(parents=True)
    lib_path = lib_dir / "libmath.so"
    lib_path.write_bytes(b"fake-elf-bytes")
    real_sha = hashlib.sha256(lib_path.read_bytes()).hexdigest()

    for rel in header_files:
        header_path = staged / "headers" / rel
        header_path.parent.mkdir(parents=True, exist_ok=True)
        header_path.write_text("// header\n")
    header_roots = sorted({f"headers/{rel.split('/')[0]}" for rel in header_files})

    build_output = {
        "schema_version": 1,
        "success": True,
        "profile": {"backend": "bazel"},
        "targets": {"math": {"kind": "shared_library", "built": True, "path": "artifacts/lib/libmath.so", "sha256": sha256 or real_sha, "size_bytes": 14}},
        "header_roots": header_roots,
        "evidence": {"dir": "evidence", "backend_evidence": backend_evidence},
    }
    (staged / "build-output.json").write_text(json.dumps(build_output))
    return staged, real_sha


def test_bazel_profile_passes_with_resolved_targets(tmp_path):
    staged, real_sha = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    result = evaluate(_BAZEL_PROFILE, staged)
    assert result["gate_status"] == "PASS", result["failures"]


def test_bazel_profile_fails_with_zero_resolved_targets(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": []}, sha256=None)
    result = evaluate(_BAZEL_PROFILE, staged)
    assert result["gate_status"] == "FAIL"
    assert any("resolved_target_count" in f for f in result["failures"])


def test_cmake_profile_fails_without_compile_commands(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "compile_commands", "compile_commands_present": False}, sha256=None)
    result = evaluate(_CMAKE_PROFILE, staged)
    assert result["gate_status"] == "FAIL"
    assert any("compile_commands.json" in f for f in result["failures"])


def test_cmake_profile_passes_with_compile_commands(tmp_path):
    cc_path = "evidence/compile_commands.json"
    staged, _ = _stage(
        tmp_path,
        backend_evidence={"kind": "compile_commands", "compile_commands_present": True, "compile_commands_path": cc_path},
        sha256=None,
    )
    (staged / "evidence").mkdir(exist_ok=True)
    (staged / cc_path).write_text(json.dumps([{"file": "src/math.cc"}]))
    result = evaluate(_CMAKE_PROFILE, staged)
    assert result["gate_status"] == "PASS", result["failures"]


def test_digest_mismatch_fails(tmp_path):
    staged, real_sha = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256="0" * 64)
    result = evaluate(_BAZEL_PROFILE, staged)
    assert result["gate_status"] == "FAIL"
    assert any("digest mismatch" in f for f in result["failures"])


def test_missing_public_headers_fails(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    build_output = json.loads((staged / "build-output.json").read_text())
    build_output["header_roots"] = []
    (staged / "build-output.json").write_text(json.dumps(build_output))
    result = evaluate(_BAZEL_PROFILE, staged)
    assert result["gate_status"] == "FAIL"
    assert any("header_roots" in f for f in result["failures"])


def _write_report(staged, name, **fields):
    report_path = staged / name
    report = {"candidate_library_path": "artifacts/lib/libmath.so", "verdict": "NO_CHANGE"}
    report.update(fields)
    report_path.write_text(json.dumps(report))
    return report_path


def test_report_cross_check_passes_with_matching_identity(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    report_path = _write_report(staged, "report.json", profile_id="p-bazel", target="math")
    result = evaluate(_BAZEL_PROFILE, staged, {"math": report_path})
    assert result["gate_status"] == "PASS", result["failures"]


def test_report_cross_check_fails_on_wrong_profile_id(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    # A report actually produced for a different profile (e.g. cmake's math
    # baseline check) but pointed at the same staged-relative path -- must
    # not be trusted just because candidate_library_path happens to match.
    report_path = _write_report(staged, "report.json", profile_id="p-cmake", target="math")
    result = evaluate(_BAZEL_PROFILE, staged, {"math": report_path})
    assert result["gate_status"] == "FAIL"
    assert any("was produced for profile" in f for f in result["failures"])


def test_report_cross_check_fails_on_wrong_target(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    report_path = _write_report(staged, "report.json", profile_id="p-bazel", target="strings")
    result = evaluate(_BAZEL_PROFILE, staged, {"math": report_path})
    assert result["gate_status"] == "FAIL"
    assert any("was produced for target" in f for f in result["failures"])


def test_report_cross_check_fails_when_target_missing_report(tmp_path):
    staged, _ = _stage(tmp_path, backend_evidence={"kind": "bazel-cquery", "targets": ["//:math"]}, sha256=None)
    result = evaluate(_BAZEL_PROFILE, staged, {})
    assert result["gate_status"] == "FAIL"
    assert any("no --report was supplied" in f for f in result["failures"])
