#!/usr/bin/env python3
"""Fail-closed check: every `gating: true` capabilities.yaml entry (in the
selected scope) must have a `status: passed` receipt
(scripts/capability_receipts.py) in the given receipts directory.

This is the read half of the phase-5 receipt mechanism -- see
capability_receipts.py's module docstring for why a receipt exists at all
and exactly what capabilities.yaml claims it proves. Three distinct
failure modes, all reported by id rather than collapsed into one generic
message:

  1. MISSING_RECEIPT -- no receipt file at all for a required capability.
     Indistinguishable, without this check, from "the job that was
     supposed to prove this silently never got that far" -- which is
     exactly the gap phase 1-4's job/workflow-existence check cannot
     close (see capability_receipts.py's docstring).
  2. NOT_PASSED -- a receipt exists but says `status: skipped` or
     `status: failed`. Reported with the receipt's own `detail` when
     present, so a skip has a stated reason attached rather than reading
     as an unexplained gap.
  3. MALFORMED_RECEIPT -- a receipt file exists but doesn't parse against
     the schema (capability_receipts.read_receipt already raises for
     this; surfaced here rather than silently ignored, since a corrupted
     "passed" receipt is exactly as untrustworthy as a missing one).

Scope is capabilities.yaml's own `gating: true` entries, optionally
narrowed further with `--capability-id` (repeatable) -- scenarios.yml
only ever has its own two ids' receipts available (a receipt written in
one workflow run isn't visible to a different workflow's run), so it
passes `--capability-id detection-correctness-scenarios-castxml
--capability-id detection-correctness-scenarios-clang` rather than
validating the full gating set abi-scan.yml's own receipts alone can
never fully satisfy either (it never produces the scenarios.yml pair).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "validate_capability_receipts: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

from capability_receipts import ReceiptError, load_all_receipts

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "capabilities.yaml"


def _required_capability_ids(matrix: dict[str, Any], only: set[str] | None) -> list[str]:
    ids = [
        entry["id"]
        for entry in matrix.get("capabilities", [])
        if entry.get("gating") is True and isinstance(entry.get("id"), str)
    ]
    if only is not None:
        unknown = only - set(ids)
        if unknown:
            raise SystemExit(
                f"validate_capability_receipts: --capability-id {sorted(unknown)} "
                "is not a gating: true entry in capabilities.yaml"
            )
        ids = [i for i in ids if i in only]
    return ids


def validate(matrix: dict[str, Any], receipts_dir: Path, only: set[str] | None) -> list[str]:
    errors: list[str] = []
    required_ids = _required_capability_ids(matrix, only)
    if not required_ids:
        errors.append(
            "NO_REQUIRED_CAPABILITIES: nothing to validate -- either "
            "capabilities.yaml has no gating: true entries, or --capability-id "
            "filtered everything out"
        )
        return errors

    try:
        receipts = load_all_receipts(receipts_dir)
    except ReceiptError as exc:
        errors.append(f"MALFORMED_RECEIPT: {exc}")
        return errors

    for capability_id in required_ids:
        receipt = receipts.get(capability_id)
        if receipt is None:
            errors.append(
                f"MISSING_RECEIPT: '{capability_id}' is gating: true but no "
                f"receipt was found under {receipts_dir}"
            )
            continue
        if receipt["status"] != "passed":
            detail = f" ({receipt['detail']})" if receipt.get("detail") else ""
            errors.append(
                f"NOT_PASSED: '{capability_id}' receipt says "
                f"status={receipt['status']!r}{detail}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=MATRIX_PATH,
        help="path to capabilities.yaml (default: repo root)",
    )
    parser.add_argument(
        "--receipts-dir",
        type=Path,
        required=True,
        help="directory containing one <capability-id>.json receipt per file",
    )
    parser.add_argument(
        "--capability-id",
        action="append",
        dest="capability_ids",
        default=None,
        help=(
            "restrict validation to this capability id (repeatable); default "
            "validates every gating: true entry in --matrix"
        ),
    )
    args = parser.parse_args()

    with args.matrix.open(encoding="utf-8") as fh:
        matrix = yaml.safe_load(fh)
    if not isinstance(matrix, dict) or "capabilities" not in matrix:
        print(f"validate_capability_receipts: {args.matrix}: expected a 'capabilities' key", file=sys.stderr)
        return 1

    only = set(args.capability_ids) if args.capability_ids else None
    errors = validate(matrix, args.receipts_dir, only)

    if errors:
        print(f"validate_capability_receipts: {len(errors)} problem(s) found:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    required_ids = _required_capability_ids(matrix, only)
    print(
        f"validate_capability_receipts: OK -- {len(required_ids)} gating "
        f"capability receipt(s) all passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
