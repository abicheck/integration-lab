#!/usr/bin/env python3
"""Write one capability receipt (scripts/capability_receipts.py) from a
workflow `run:` step -- the emission half of the phase-5 receipt
mechanism. See capability_receipts.py's module docstring for the schema
and rationale.

Usage (from a GitHub Actions step):

    python3 scripts/emit_capability_receipt.py \\
      --receipts-dir "$RUNNER_TEMP/capability-receipts" \\
      --capability-id math-source-gate \\
      --status passed \\
      --workflow abi-scan.yml \\
      --job scan \\
      --run-id "${{ github.run_id }}" \\
      --run-attempt "${{ github.run_attempt }}" \\
      --sha "${{ github.sha }}"

Deliberately a thin CLI, not a composite action: every call site already
runs Python (this repo's other lab scripts do too), and a receipt is one
`json.dump` -- a composite action would add indirection without adding
anything a plain `run:` step calling this script doesn't already give.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from capability_receipts import ReceiptError, build_receipt, write_receipt

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts-dir", type=Path, required=True)
    parser.add_argument("--capability-id", required=True)
    parser.add_argument("--status", required=True, choices=["passed", "failed", "skipped"])
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-attempt", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--detail", default="")
    args = parser.parse_args()

    try:
        receipt = build_receipt(
            capability_id=args.capability_id,
            status=args.status,
            workflow=args.workflow,
            job=args.job,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            sha=args.sha,
            detail=args.detail,
        )
        path = write_receipt(args.receipts_dir, receipt)
    except ReceiptError as exc:
        print(f"emit_capability_receipt: {exc}", file=sys.stderr)
        return 1

    print(f"emit_capability_receipt: wrote {path} (status={args.status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
