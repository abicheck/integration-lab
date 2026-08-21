"""Unit tests for ci/render_integration_gate.py (PR2 item 3) -- the final
advisory integration-gate job's own validation logic.
"""
from __future__ import annotations

import json

from render_integration_gate import evaluate


def _write_receipt(dir_path, profile_id, status="passed", verdicts=None, extra=None):
    verdicts = verdicts or {"math": "NO_CHANGE"}
    receipt = {
        "schema_version": 1,
        "kind": "abicheck.integration-profile-receipt/v1",
        "profile_id": profile_id,
        "required": False,
        "status": status,
        "workflow": "wf.yml",
        "job": "build",
        "run_id": "1",
        "run_attempt": "1",
        "sha": "abc",
        "build": {"success": True},
        "scanner": {"mechanism": "nm/readelf"},
        "build_system": "cmake",
        "compiler": {},
        "build_output_digest": "deadbeef",
        "reports": [{"target": t, "path": "p", "digest": "d", "verdict": v} for t, v in verdicts.items()],
        "coverage": None,
        "detail": "",
    }
    if extra:
        receipt.update(extra)
    (dir_path / f"{profile_id}.json").write_text(json.dumps(receipt))


def test_gate_passes_when_all_expected_receipts_are_clean(tmp_path):
    _write_receipt(tmp_path, "p1")
    _write_receipt(tmp_path, "p2")
    result = evaluate(["p1", "p2"], tmp_path)
    assert result["gate_status"] == "PASS", result["failures"]


def test_gate_fails_on_missing_receipt(tmp_path):
    _write_receipt(tmp_path, "p1")
    result = evaluate(["p1", "p2"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any("p2" in f and "no receipt" in f for f in result["failures"])


def test_gate_fails_on_failed_status(tmp_path):
    _write_receipt(tmp_path, "p1", status="failed")
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"


def test_gate_fails_on_breaking_verdict_even_if_status_passed(tmp_path):
    _write_receipt(tmp_path, "p1", status="passed", verdicts={"math": "BREAKING"})
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"


def test_gate_fails_on_schema_invalid_receipt(tmp_path):
    (tmp_path / "p1.json").write_text(json.dumps({"profile_id": "p1"}))
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any("schema validation" in f for f in result["failures"])


def test_gate_fails_on_receipt_with_wrong_embedded_profile_id(tmp_path):
    # p1.json is schema-valid and status=passed -- but it's actually a
    # receipt for p2 (a copied or misnamed artifact download). The
    # filename alone must not be trusted as proof of which profile this
    # receipt covers.
    _write_receipt(tmp_path, "p2")
    (tmp_path / "p1.json").write_text((tmp_path / "p2.json").read_text())
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any("is for profile_id='p2'" in f for f in result["failures"])


def test_gate_fails_on_non_utf8_receipt(tmp_path):
    # A corrupted/binary artifact download raises UnicodeDecodeError, not
    # json.JSONDecodeError -- UnicodeDecodeError is a ValueError, so it
    # previously escaped the JSONDecodeError-only handler entirely and
    # crashed this whole gate job instead of producing the INVALID row
    # this branch exists to report.
    (tmp_path / "p1.json").write_bytes(b"\xff\xfe\x00\xff not valid utf-8 or json")
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any(row["status"] == "INVALID" for row in result["rows"])


def test_gate_fails_on_status_passed_but_build_not_success(tmp_path):
    # A schema-valid receipt claiming status=passed while its own build
    # evidence says success=false must not be trusted on the status field
    # alone -- the two are contradictory and the underlying facts win.
    _write_receipt(tmp_path, "p1", extra={"build": {"success": False}})
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any("build.success is not true" in f for f in result["failures"])


def test_gate_fails_on_status_passed_but_coverage_failed(tmp_path):
    _write_receipt(tmp_path, "p1", extra={"coverage": {"gate_status": "FAIL", "failures": ["nope"]}})
    result = evaluate(["p1"], tmp_path)
    assert result["gate_status"] == "FAIL"
    assert any("coverage.gate_status='FAIL'" in f for f in result["failures"])
