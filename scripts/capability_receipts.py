#!/usr/bin/env python3
"""Shared schema + I/O for capability *receipts* (phase 5 of the
declarative capability-matrix plan -- see capabilities.yaml's own header
comment and README.md's "Capability matrix" section for phases 1-4).

## Why this exists

`scripts/check_capability_matrix.py` (phases 1/2/4) proves capabilities.yaml
stays honest about *what CI is wired to run* -- every `covered`/
`non_gating_watch` entry names a real `job` in a real workflow, and every
real job is named by some entry. It cannot prove the one thing that
actually matters for trusting a `gating: true` entry: that the job ran
*this workflow run* and actually reached a passing result, rather than the
step being silently skipped, degraded, or failing open. A capability
entry pointing at a job that exists says nothing about whether that job's
gating step executed to completion on the run a required check is being
judged against.

A receipt closes that gap: a small, machine-written JSON fact, one per
`gating: true` capability id, saying "this run, this workflow, this job
produced this status" -- written by the job itself right after the step(s)
that capability's capabilities.yaml entry describes, and checked by
`scripts/validate_capability_receipts.py` before anything downstream
trusts it. This is deliberately narrower than the "runtime capability
receipts" design sketched for a full downstream conformance platform (a
receipt naming a scanner ref, an effective-config digest, semantic
assertions on the report's own findings, ...) -- this phase only closes
the "was it actually executed and did it pass" gap for the capabilities
that already gate a PR (`gating: true` in capabilities.yaml today: exactly
4 -- math-source-gate, aggregate-multi-library,
detection-correctness-scenarios-{castxml,clang}). Extending the schema to
carry scanner/report provenance is a natural next phase, not attempted
here -- see UPSTREAM_TO_ABICHECK.md's own staging discipline for why this
module keeps the receipt shape minimal rather than speculatively wide.

## Schema

    {
      "schema_version": 1,
      "capability_id": "math-source-gate",
      "status": "passed" | "failed" | "skipped",
      "workflow": "abi-scan.yml",
      "job": "scan",
      "run_id": "1234567890",
      "run_attempt": "1",
      "sha": "abcdef0123...",
      "detail": "optional free-text explanation, e.g. why status=skipped"
    }

`status: skipped` is a real, distinct value -- not the absence of a
receipt. A capability whose gating step never ran this PR (e.g.
`math-source-gate` when `skip-check` judged the PR not ABI-relevant) still
writes a receipt saying so, so the validator can tell "genuinely nothing
to prove this run" apart from "the job silently produced nothing" -- the
exact ambiguity a *missing* receipt cannot resolve on its own. Only
`status: passed` satisfies the validator for a `gating: true` capability;
`skipped`/`failed` are both reported, deliberately not treated the same
(the validator's message names which).

No timestamp field: this module is called from workflow scripts where
`date -u` output would only ever be advisory (receipts aren't compared
across runs by time), and every fact that matters for provenance --
`run_id`/`run_attempt`/`sha` -- is already carried explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

VALID_STATUSES = frozenset({"passed", "failed", "skipped"})


class ReceiptError(ValueError):
    """A receipt (or a receipt file on disk) doesn't match the schema."""


def build_receipt(
    *,
    capability_id: str,
    status: str,
    workflow: str,
    job: str,
    run_id: str = "",
    run_attempt: str = "",
    sha: str = "",
    detail: str = "",
) -> dict[str, Any]:
    if not capability_id:
        raise ReceiptError("capability_id must be a non-empty string")
    if status not in VALID_STATUSES:
        raise ReceiptError(f"status={status!r} must be one of {sorted(VALID_STATUSES)}")
    if not workflow:
        raise ReceiptError("workflow must be a non-empty string")
    if not job:
        raise ReceiptError("job must be a non-empty string")
    return {
        "schema_version": SCHEMA_VERSION,
        "capability_id": capability_id,
        "status": status,
        "workflow": workflow,
        "job": job,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "sha": sha,
        "detail": detail,
    }


def receipt_path(receipts_dir: Path, capability_id: str) -> Path:
    # One file per capability id, named after the id itself -- the id is
    # already a slug (capabilities.yaml's own `id:` values), so no further
    # sanitizing is needed, and this keeps "does a receipt exist for X"
    # a plain path check rather than requiring every reader to scan and
    # filter a directory of arbitrarily-named files.
    return receipts_dir / f"{capability_id}.json"


def write_receipt(receipts_dir: Path, receipt: dict[str, Any]) -> Path:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_path(receipts_dir, receipt["capability_id"])
    with path.open("w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def read_receipt(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{path}: unreadable or not valid JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise ReceiptError(f"{path}: expected a JSON object")

    missing = [k for k in ("schema_version", "capability_id", "status", "workflow", "job") if k not in data]
    if missing:
        raise ReceiptError(f"{path}: missing required field(s) {missing}")

    if data["schema_version"] != SCHEMA_VERSION:
        raise ReceiptError(
            f"{path}: schema_version={data['schema_version']!r}, "
            f"this reader only understands {SCHEMA_VERSION}"
        )
    if data["status"] not in VALID_STATUSES:
        raise ReceiptError(f"{path}: status={data['status']!r} not in {sorted(VALID_STATUSES)}")
    return data


def load_all_receipts(receipts_dir: Path) -> dict[str, dict[str, Any]]:
    """{capability_id: receipt} for every *.json file under receipts_dir.

    A malformed receipt is a hard error, not skipped -- a receipt that
    fails to parse is exactly as untrustworthy as a missing one, and
    silently dropping it would let a corrupted "passed" receipt (or a
    receipt some other, unrelated tool wrote into the same directory)
    pass through as if nothing was there to check.
    """
    if not receipts_dir.is_dir():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(receipts_dir.glob("*.json")):
        receipt = read_receipt(path)
        stem_id = path.stem
        if receipt["capability_id"] != stem_id:
            raise ReceiptError(
                f"{path}: capability_id={receipt['capability_id']!r} does not "
                f"match filename stem {stem_id!r}"
            )
        if stem_id in result:
            raise ReceiptError(f"duplicate receipt for capability id {stem_id!r}")
        result[stem_id] = receipt
    return result
