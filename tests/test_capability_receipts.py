"""Unit tests for scripts/capability_receipts.py -- the receipt schema and
read/write primitives every emitter and the validator build on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capability_receipts import (
    ReceiptError,
    build_receipt,
    load_all_receipts,
    read_receipt,
    receipt_path,
    write_receipt,
)


def test_build_receipt_round_trips_required_fields():
    receipt = build_receipt(
        capability_id="math-source-gate",
        status="passed",
        workflow="abi-scan.yml",
        job="scan",
        run_id="123",
        run_attempt="1",
        sha="deadbeef",
    )
    assert receipt["schema_version"] == 1
    assert receipt["capability_id"] == "math-source-gate"
    assert receipt["status"] == "passed"
    assert receipt["workflow"] == "abi-scan.yml"
    assert receipt["job"] == "scan"
    assert receipt["run_id"] == "123"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(capability_id="", status="passed", workflow="w.yml", job="j"),
        dict(capability_id="x", status="bogus", workflow="w.yml", job="j"),
        dict(capability_id="x", status="passed", workflow="", job="j"),
        dict(capability_id="x", status="passed", workflow="w.yml", job=""),
    ],
)
def test_build_receipt_rejects_invalid_input(kwargs):
    with pytest.raises(ReceiptError):
        build_receipt(**kwargs)


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "a/b", "a\\b", ".", "..", "", "id with spaces"],
)
def test_build_receipt_rejects_path_unsafe_capability_id(bad_id):
    # Codex/CodeRabbit review, fresh evidence: capability_id is embedded
    # directly into receipt_path()'s output path -- an unvalidated value
    # would let write_receipt() escape receipts_dir entirely.
    with pytest.raises(ReceiptError):
        build_receipt(capability_id=bad_id, status="passed", workflow="w.yml", job="j")


def test_receipt_path_rejects_path_unsafe_capability_id_directly(tmp_path):
    # Protects a direct write_receipt()/receipt_path() caller too, not
    # only ones that go through build_receipt() first.
    with pytest.raises(ReceiptError):
        receipt_path(tmp_path, "../escape")


def test_write_receipt_cannot_escape_receipts_dir(tmp_path):
    # End-to-end: even a hand-built receipt dict (bypassing build_receipt's
    # own validation) must not let write_receipt() write outside
    # receipts_dir.
    escaping = {
        "schema_version": 1,
        "capability_id": "../escape",
        "status": "passed",
        "workflow": "w.yml",
        "job": "j",
        "run_id": "",
        "run_attempt": "",
        "sha": "",
        "detail": "",
    }
    with pytest.raises(ReceiptError):
        write_receipt(tmp_path, escaping)
    assert not (tmp_path.parent / "escape.json").exists()


def test_build_receipt_rejects_unhashable_status():
    # Codex review, fresh evidence: `status not in VALID_STATUSES` alone
    # raises TypeError (not ReceiptError) for an unhashable value like a
    # list -- must degrade to the same MALFORMED_RECEIPT-shaped error
    # every other bad field already produces.
    with pytest.raises(ReceiptError):
        build_receipt(capability_id="x", status=["passed"], workflow="w.yml", job="j")


def test_read_receipt_rejects_unhashable_status(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_id": "x",
                "status": ["passed"],
                "workflow": "w.yml",
                "job": "j",
            }
        )
    )
    with pytest.raises(ReceiptError):
        read_receipt(path)


def test_write_then_read_round_trip(tmp_path):
    receipt = build_receipt(
        capability_id="aggregate-multi-library",
        status="failed",
        workflow="abi-scan.yml",
        job="aggregate",
        detail="expected-target manifest not satisfied",
    )
    path = write_receipt(tmp_path, receipt)
    assert path == receipt_path(tmp_path, "aggregate-multi-library")
    assert path.is_file()

    loaded = read_receipt(path)
    assert loaded == receipt


def test_read_receipt_rejects_missing_required_field(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"schema_version": 1, "capability_id": "broken"}))
    with pytest.raises(ReceiptError, match="missing required field"):
        read_receipt(path)


def test_read_receipt_rejects_future_schema_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "capability_id": "future",
                "status": "passed",
                "workflow": "w.yml",
                "job": "j",
            }
        )
    )
    with pytest.raises(ReceiptError, match="schema_version"):
        read_receipt(path)


def test_read_receipt_rejects_bad_status(tmp_path):
    path = tmp_path / "bad-status.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability_id": "bad-status",
                "status": "maybe",
                "workflow": "w.yml",
                "job": "j",
            }
        )
    )
    with pytest.raises(ReceiptError, match="status"):
        read_receipt(path)


def test_read_receipt_rejects_non_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ReceiptError, match="expected a JSON object"):
        read_receipt(path)


def test_read_receipt_rejects_unreadable_file(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ReceiptError, match="unreadable"):
        read_receipt(missing)


def test_load_all_receipts_empty_dir_missing_dir(tmp_path):
    assert load_all_receipts(tmp_path / "nonexistent") == {}
    assert load_all_receipts(tmp_path) == {}


def test_load_all_receipts_collects_every_file(tmp_path):
    write_receipt(
        tmp_path,
        build_receipt(capability_id="a", status="passed", workflow="w.yml", job="j"),
    )
    write_receipt(
        tmp_path,
        build_receipt(capability_id="b", status="skipped", workflow="w.yml", job="j"),
    )
    receipts = load_all_receipts(tmp_path)
    assert set(receipts) == {"a", "b"}
    assert receipts["a"]["status"] == "passed"
    assert receipts["b"]["status"] == "skipped"


def test_load_all_receipts_rejects_filename_id_mismatch(tmp_path):
    # A receipt whose own capability_id doesn't match the filename it was
    # found at is exactly as untrustworthy as one that doesn't parse --
    # this is what stops a copy/rename of one capability's receipt from
    # silently satisfying a different capability's requirement.
    mismatched = build_receipt(capability_id="a", status="passed", workflow="w.yml", job="j")
    path = tmp_path / "b.json"
    path.write_text(json.dumps(mismatched))
    with pytest.raises(ReceiptError, match="does not match filename"):
        load_all_receipts(tmp_path)


# --------------------------------------------------------------------------
# Item 15: the matrix must cover the native project and depth-scenario paths
# --------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


def _matrix() -> dict:
    import yaml

    return yaml.safe_load(
        (REPO_ROOT / "capabilities.yaml").read_text(encoding="utf-8")
    )


def test_matrix_covers_the_native_project_and_depth_workflows():
    """capabilities.yaml claimed to be the coverage inventory while modelling
    only the older scan/scenario suite; every native-project and
    evidence-depth job was invisible to it."""
    workflows = {entry.get("workflow") for entry in _matrix()["capabilities"]}
    assert "project-shadow.yml" in workflows
    assert "depth-scenarios.yml" in workflows


def test_every_declared_job_still_exists():
    """The UNDOCUMENTED_JOB / STALE_ENTRY contract, run as a unit test."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_capability_matrix", REPO_ROOT / "scripts" / "check_capability_matrix.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check(_matrix()) == []


def test_new_scenario_families_are_declared():
    ids = {entry["id"] for entry in _matrix()["capabilities"]}
    for required in (
        "producer-compiler-attribution",
        "loader-feature-drift",
        "native-project-baseline-identity",
        "native-run-plan-semantics",
    ):
        assert required in ids, required
