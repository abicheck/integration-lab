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
