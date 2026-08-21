"""Unit tests for ci/emit_profile_receipt.py (PR2 item 2) and, since it's
the same JSON-shape contract this receipt exists to satisfy,
ci/schemas/profile-receipt.schema.json via ci/validate_build_output.py's
reusable validator.
"""
from __future__ import annotations

import json
from pathlib import Path

from emit_profile_receipt import build_receipt
from validate_build_output import validate_document

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "ci" / "schemas" / "profile-receipt.schema.json"


def _stage(tmp_path, success=True):
    staged = tmp_path / "staged"
    staged.mkdir()
    build_output = {
        "success": success,
        "profile": {"backend": "cmake"},
        "compiler": {"family": "gcc"},
    }
    (staged / "build-output.json").write_text(json.dumps(build_output))
    return staged


def _report(tmp_path, name, verdict):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"verdict": verdict, "candidate_library_path": "artifacts/lib/libmath.so"}))
    return path


def test_receipt_passed_when_build_ok_and_verdicts_clean(tmp_path):
    staged = _stage(tmp_path)
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE")}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "passed"
    assert receipt["reports"][0]["verdict"] == "NO_CHANGE"
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_failed_on_breaking_verdict(tmp_path):
    staged = _stage(tmp_path)
    report_paths = {"math": _report(tmp_path, "math", "BREAKING")}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_failed_when_build_did_not_succeed(tmp_path):
    staged = _stage(tmp_path, success=False)
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE")}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert receipt["build"]["success"] is False


def test_receipt_failed_when_report_missing(tmp_path):
    staged = _stage(tmp_path)
    report_paths = {"math": tmp_path / "does-not-exist.json"}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert receipt["reports"][0]["verdict"] == "NOT_RUN"


def test_receipt_failed_when_coverage_fails(tmp_path):
    staged = _stage(tmp_path)
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE")}
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps({"gate_status": "FAIL", "failures": ["nope"]}))
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=coverage_path,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
