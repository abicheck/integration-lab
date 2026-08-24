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


def _report(tmp_path, name, verdict, mechanism=None):
    path = tmp_path / f"{name}.json"
    doc = {"verdict": verdict, "candidate_library_path": "artifacts/lib/libmath.so"}
    if mechanism is not None:
        doc["mechanism"] = mechanism
    path.write_text(json.dumps(doc))
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


def test_receipt_prefers_legacy_sidecar_for_execution_fields(tmp_path):
    staged = _stage(tmp_path)
    (staged / "lab-build-output.json").write_text(json.dumps({
        "success": True,
        "profile": {"backend": "make"},
        "compiler": {"family": "gcc", "cxx": "g++-14"},
    }))
    (staged / "build-output.json").write_text(json.dumps({
        "schema": "abicheck.build-output/v1",
        "profile": {"id": "p1", "compiler": {"family": "gcc"}},
        "targets": [],
    }))
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged,
        report_paths={"math": _report(tmp_path, "math", "NO_CHANGE")},
        coverage_result=None, workflow="wf.yml", job="build", run_id="1",
        run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "passed"
    assert receipt["build_system"] == "make"
    assert receipt["compiler"]["cxx"] == "g++-14"


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


def test_receipt_failed_when_report_is_non_object_json(tmp_path):
    # A report file can be valid JSON with a non-object top level (e.g. a
    # truncated/corrupted write leaving `[]`) -- this must fall through to
    # NOT_RUN, not crash the always-running receipt step with an
    # AttributeError from calling .get() on a list.
    staged = _stage(tmp_path)
    report_path = tmp_path / "math.json"
    report_path.write_text(json.dumps([]))
    report_paths = {"math": report_path}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert receipt["reports"][0]["verdict"] == "NOT_RUN"
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_failed_when_build_output_is_non_object_json(tmp_path):
    # A failed/corrupted profile run can leave build-output.json as
    # syntactically valid but non-object JSON (e.g. `[]`) -- this must
    # fall through to the same "no valid build-output.json" handling as
    # a missing/unparseable file, not crash with AttributeError.
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "build-output.json").write_text(json.dumps([]))
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE")}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert receipt["build"]["success"] is False
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_failed_when_coverage_result_is_non_object_json(tmp_path):
    staged = _stage(tmp_path)
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE")}
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(json.dumps([]))
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=coverage_path,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["status"] == "failed"
    assert validate_document(receipt, SCHEMA_PATH) == []


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


def test_receipt_scanner_mechanism_prefers_recorded_report_mechanism(tmp_path):
    # ci/check_profile.py stamps each report's own facts["mechanism"] with
    # whichever mechanism actually produced ITS verdict -- including on a
    # fail-closed branch, which can legitimately differ from what a
    # backend-derived guess would assume (Codex review, PR #25, round 3:
    # "Deriving scanner.mechanism from backend reintroduces misattribution
    # in the opposite direction"). A recorded mechanism must win over the
    # backend-derived guess, not just supplement it.
    staged = _stage(tmp_path)  # backend "cmake" -- would otherwise guess "real abicheck ..."
    report_paths = {"math": _report(tmp_path, "math", "NO_CHANGE", mechanism="a distinctive recorded mechanism")}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["scanner"]["mechanism"] == "a distinctive recorded mechanism"
    assert receipt["reports"][0]["mechanism"] == "a distinctive recorded mechanism"
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_scanner_mechanism_joins_distinct_mechanisms_across_targets(tmp_path):
    # A single profile run's reports can legitimately have been produced by
    # different mechanisms (e.g. a make-backend profile's bear-absent
    # degrade for one target, real abicheck for another) -- the receipt
    # must surface both, not silently collapse to one.
    staged = _stage(tmp_path)
    report_paths = {
        "math": _report(tmp_path, "math", "NO_CHANGE", mechanism="mechanism A"),
        "strings": _report(tmp_path, "strings", "NO_CHANGE", mechanism="mechanism B"),
    }
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["scanner"]["mechanism"] == "mechanism A; mechanism B"
    assert validate_document(receipt, SCHEMA_PATH) == []


def test_receipt_scanner_mechanism_falls_back_to_backend_guess_when_unreadable(tmp_path):
    # No report is readable at all (e.g. every report file is missing) --
    # the backend-derived guess is the only signal left, so it's still
    # used rather than leaving scanner.mechanism empty.
    staged = _stage(tmp_path)  # backend "cmake" -- in _REAL_SCAN_BACKENDS
    report_paths = {"math": tmp_path / "does-not-exist.json"}
    receipt = build_receipt(
        profile_id="p1", staged_dir=staged, report_paths=report_paths, coverage_result=None,
        workflow="wf.yml", job="build", run_id="1", run_attempt="1", sha="abc", required=False,
    )
    assert receipt["reports"][0]["mechanism"] is None
    assert "real abicheck dump/compare" in receipt["scanner"]["mechanism"]
    assert validate_document(receipt, SCHEMA_PATH) == []
