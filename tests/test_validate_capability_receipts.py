"""Unit tests for scripts/validate_capability_receipts.py -- the fail-closed
check that every gating: true capability has an acceptable, provenance-
verified receipt.
"""

from __future__ import annotations

import pytest

from capability_receipts import build_receipt, write_receipt
from validate_capability_receipts import validate

MATRIX = {
    "capabilities": [
        {"id": "gating-a", "gating": True, "workflow": "w.yml", "job": "job-a"},
        {"id": "gating-b", "gating": True, "workflow": "w.yml", "job": "job-b"},
        {"id": "watch-only", "gating": False, "workflow": "w.yml", "job": "job-a"},
        {"id": "no-gating-key"},
    ]
}


def _write(
    tmp_path,
    capability_id,
    status,
    job=None,
    run_id="run-1",
    run_attempt="1",
    sha="deadbeef",
    **kwargs,
):
    write_receipt(
        tmp_path,
        build_receipt(
            capability_id=capability_id,
            status=status,
            workflow="w.yml",
            job=job or f"job-{capability_id.rsplit('-', 1)[-1]}",
            run_id=run_id,
            run_attempt=run_attempt,
            sha=sha,
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


def test_skipped_receipt_without_allow_skip_fails(tmp_path):
    # Codex review, fresh evidence: skip-tolerance must be opt-in per id,
    # not a blanket default -- a capability with no legitimate "not
    # applicable this run" condition (e.g. a scenario-detection profile)
    # must not silently pass just because it happened to skip.
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "skipped", detail="manifest declared nothing for this profile")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "UNEXPECTED_SKIP" in errors[0]
    assert "gating-b" in errors[0]
    assert "manifest declared nothing for this profile" in errors[0]


def test_skipped_receipt_with_allow_skip_is_accepted(tmp_path):
    # A legitimate skip (e.g. skip-check judged the PR not ABI-relevant)
    # must not fail this validator when the caller explicitly opted this
    # id into skip-tolerance -- abi-scan.yml's own "Enforce gate" step
    # already treats math-source-gate's skip as clean.
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "skipped", detail="PR not ABI-relevant")
    assert validate(MATRIX, tmp_path, only=None, allow_skip={"gating-b"}) == []


def test_failed_receipt_for_gating_capability_fails(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "failed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "FAILED" in errors[0]


def test_failed_receipt_fails_even_when_allow_skip_names_it(tmp_path):
    # --allow-skip only tolerates status: skipped -- it must never
    # downgrade a real status: failed into an accepted outcome.
    _write(tmp_path, "gating-a", "passed")
    _write(tmp_path, "gating-b", "failed")
    errors = validate(MATRIX, tmp_path, only=None, allow_skip={"gating-b"})
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
    with pytest.raises(SystemExit):
        validate(MATRIX, tmp_path, only={"does-not-exist"})


def test_allow_skip_id_outside_scope_is_reported(tmp_path):
    _write(tmp_path, "gating-a", "passed")
    errors = validate(MATRIX, tmp_path, only={"gating-a"}, allow_skip={"gating-b"})
    assert len(errors) == 1
    assert "BAD_ALLOW_SKIP" in errors[0]
    assert "gating-b" in errors[0]


def test_malformed_receipt_reported(tmp_path):
    (tmp_path / "gating-a.json").write_text("not json")
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MALFORMED_RECEIPT" in errors[0]


# --- Provenance checks (Codex review, fresh evidence) ---


def test_receipt_from_wrong_workflow_rejected(tmp_path):
    write_receipt(
        tmp_path,
        build_receipt(
            capability_id="gating-a",
            status="passed",
            workflow="some-other.yml",  # capabilities.yaml declares w.yml
            job="job-a",
            run_id="run-1",
            sha="deadbeef",
        ),
    )
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "WRONG_JOB_PROVENANCE" in errors[0]
    assert "gating-a" in errors[0]


def test_receipt_from_wrong_job_rejected(tmp_path):
    write_receipt(
        tmp_path,
        build_receipt(
            capability_id="gating-a",
            status="passed",
            workflow="w.yml",
            job="job-b",  # capabilities.yaml declares job-a for gating-a
            run_id="run-1",
            sha="deadbeef",
        ),
    )
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "WRONG_JOB_PROVENANCE" in errors[0]
    assert "gating-a" in errors[0]


def test_receipt_missing_run_id_rejected(tmp_path):
    _write(tmp_path, "gating-a", "passed", run_id="")
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MISSING_PROVENANCE" in errors[0]
    assert "gating-a" in errors[0]


def test_receipt_missing_sha_rejected(tmp_path):
    _write(tmp_path, "gating-a", "passed", sha="")
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MISSING_PROVENANCE" in errors[0]
    assert "gating-a" in errors[0]


def test_receipt_missing_run_attempt_rejected(tmp_path):
    _write(tmp_path, "gating-a", "passed", run_attempt="")
    _write(tmp_path, "gating-b", "passed")
    errors = validate(MATRIX, tmp_path, only=None)
    assert len(errors) == 1
    assert "MISSING_PROVENANCE" in errors[0]
    assert "gating-a" in errors[0]


def test_stale_run_id_rejected_when_expected(tmp_path):
    _write(tmp_path, "gating-a", "passed", run_id="old-run")
    _write(tmp_path, "gating-b", "passed", run_id="old-run")
    errors = validate(MATRIX, tmp_path, only=None, expect_run_id="current-run")
    assert len(errors) == 2
    assert all("STALE_PROVENANCE" in e for e in errors)


def test_stale_run_attempt_rejected_even_with_matching_run_id(tmp_path):
    # A re-run keeps the same run_id but increments run_attempt -- a
    # receipt from an earlier attempt of the identical run must still be
    # rejected when the caller pins the current attempt (Codex review,
    # fresh evidence).
    _write(tmp_path, "gating-a", "passed", run_id="run-1", run_attempt="1")
    _write(tmp_path, "gating-b", "passed", run_id="run-1", run_attempt="1")
    errors = validate(
        MATRIX,
        tmp_path,
        only=None,
        expect_run_id="run-1",
        expect_run_attempt="2",
    )
    assert len(errors) == 2
    assert all("STALE_PROVENANCE" in e and "run_attempt" in e for e in errors)


def test_stale_sha_rejected_when_expected(tmp_path):
    _write(tmp_path, "gating-a", "passed", sha="oldsha")
    _write(tmp_path, "gating-b", "passed", sha="oldsha")
    errors = validate(MATRIX, tmp_path, only=None, expect_sha="newsha")
    assert len(errors) == 2
    assert all("STALE_PROVENANCE" in e for e in errors)


def test_matching_run_id_run_attempt_and_sha_accepted(tmp_path):
    _write(tmp_path, "gating-a", "passed", run_id="run-1", run_attempt="1", sha="deadbeef")
    _write(tmp_path, "gating-b", "passed", run_id="run-1", run_attempt="1", sha="deadbeef")
    errors = validate(
        MATRIX,
        tmp_path,
        only=None,
        expect_run_id="run-1",
        expect_run_attempt="1",
        expect_sha="deadbeef",
    )
    assert errors == []


def test_no_workflow_job_declared_skips_that_check(tmp_path):
    # A capability entry that (in a test fixture, or theoretically) omits
    # workflow/job entirely has nothing to cross-check a receipt's own
    # workflow/job against -- must not spuriously fail on that alone, only
    # on the run_id/sha provenance checks which are always active.
    matrix = {"capabilities": [{"id": "no-job-decl", "gating": True}]}
    _write(tmp_path, "no-job-decl", "passed", job="whatever-job")
    assert validate(matrix, tmp_path, only=None) == []
