"""Unit tests for scripts/validate_capability_receipts.py -- the fail-closed
check that every gating: true capability has a status: passed receipt.
"""

from __future__ import annotations

from capability_receipts import build_receipt, write_receipt
from validate_capability_receipts import validate

MATRIX = {
    "capabilities": [
        {"id": "gating-a", "gating": True},
        {"id": "gating-b", "gating": True},
        {"id": "watch-only", "gating": False},
        {"id": "no-gating-key"},
    ]
}


def _write(tmp_path, capability_id, status, **kwargs):
    write_receipt(
        tmp_path,
        build_receipt(
            capability_id=capability_id,
            status=status,
            workflow="w.yml",
            job="j",
            **kwargs,
        ),
    )


def test_all_gating_capabilities_passed_is_clean(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "passed")
    assert validate(MATRIX, tmp_path, only=None) == []


def test_missing_receipt_for_gating_capability_fails(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    # gating-b has no receipt at all.
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MISSING_RECEIPT" in errors[0]
    assert "gating-b" in errors[0]


def test_skipped_receipt_for_gating_capability_is_accepted(tmp_path):
    # A legitimate skip (e.g. skip-check judged the PR not ABI-relevant)
    # must not fail this validator -- abi-scan.yml's own "Enforce gate"
    # step already treats a skip as clean, and this validator must not be
    # stricter than the gate it validates (Codex review, fresh evidence).
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "skipped", detail="PR not ABI-relevant")
    assert validate(MATRIX, tmp_path, only=None) == []


def test_failed_receipt_for_gating_capability_fails(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "failed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "FAILED" in errors[0]


def test_non_gating_capability_never_required(tmp_path):
    # watch-only and no-gating-key are never gating: true -- their absence
    # from the receipts dir is not an error, even with an otherwise-empty
    # receipts dir for the two real gating capabilities missing too.
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 2  # gating-a and gating-b both missing
    assert all("watch-only" not in e and "no-gating-key" not in e for e in errors)


def test_capability_id_filter_narrows_scope(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    # gating-b has no receipt, but it's filtered out of scope.
    errors = validate(MATRIX, tmp_path, only={"gating-a"})
    assert errors == []


def test_unknown_capability_id_filter_raises(tmp_path):
    import pytest

    with pytest.raises(SystemExit):
        validate(MATRIX, tmp_path, only={"does-not-exist"})


def test_malformed_receipt_reported(tmp_path):
    (tmp_path / "gating-a.json").write_text("not json")
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MALFORMED_RECEIPT" in errors[0]
