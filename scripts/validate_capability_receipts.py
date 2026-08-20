#!/usr/bin/env python3
"""Fail-closed check: every `gating: true` capabilities.yaml entry (in the
selected scope) must have a receipt (scripts/capability_receipts.py) in
the given receipts directory, and that receipt must say `status: passed`
-- or `status: skipped`, but ONLY for a capability id explicitly named via
`--allow-skip`.

This is the read half of the phase-5 receipt mechanism -- see
capability_receipts.py's module docstring for why a receipt exists at all
and exactly what capabilities.yaml claims it proves. Four distinct failure
modes, all reported by id rather than collapsed into one generic message:

  1. MISSING_RECEIPT -- no receipt file at all for a required capability.
     Indistinguishable, without this check, from "the job that was
     supposed to prove this silently never got that far" -- which is
     exactly the gap phase 1-4's job/workflow-existence check cannot
     close (see capability_receipts.py's docstring).
  2. FAILED -- a receipt exists and says `status: failed`. Reported with
     the receipt's own `detail` when present.
  3. UNEXPECTED_SKIP -- a receipt exists and says `status: skipped`, but
     this capability id was not named via `--allow-skip`. `status:
     skipped` is legitimate for SOME capabilities (e.g. math-source-gate
     on a PR skip-check judges not ABI-relevant -- abi-scan.yml's own
     "Enforce gate" step already treats that as a clean, non-failing
     outcome, and this validator must not be stricter than the gate it
     validates) but not others: detection-correctness-scenarios-{castxml,
     clang} have no analogous "not applicable this run" condition --
     scenarios.yml's `scenarios` job always runs every declared scenario,
     so a `skipped` receipt there means every scenario for that profile
     was silently removed from scenarios/manifest.yaml, which must fail
     loudly rather than pass vacuously (Codex review, fresh evidence: the
     first cut of this script accepted `skipped` for every capability
     unconditionally, which let exactly this go silently green). Callers
     opt a capability id into skip-tolerance explicitly, per id, rather
     than this script guessing from the id's name or its own defaults.
  4. MALFORMED_RECEIPT -- a receipt file exists but doesn't parse against
     the schema (capability_receipts.read_receipt already raises for
     this; surfaced here rather than silently ignored, since a corrupted
     "passed" receipt is exactly as untrustworthy as a missing one).

Scope is capabilities.yaml's own `gating: true` entries, optionally
narrowed further with `--capability-id` (repeatable) -- scenarios.yml
only ever has its own two ids' receipts available (a receipt written in
one workflow run isn't visible to a different workflow's run), so it
passes `--capability-id detection-correctness-scenarios-castxml
--capability-id detection-correctness-scenarios-clang` (with no
`--allow-skip` at all) rather than validating the full gating set
abi-scan.yml's own receipts alone can never fully satisfy either (it
never produces the scenarios.yml pair). abi-scan.yml's own call passes
`--allow-skip math-source-gate --allow-skip aggregate-multi-library` --
see emit_capability_receipt.py's call sites in that workflow for exactly
which real (job-conclusion-affecting) conditions each one's `skipped`
status corresponds to.

NOTE on the residual gap this script cannot close on its own: this
verifier is a separate job/step from the `scan`/`aggregate` jobs whose
receipts it reads, so its own failure only blocks a merge if it is itself
added to the repository's required status checks in branch protection --
the same "only has teeth once branch protection requires it" caveat
`.github/CODEOWNERS` already states for the checks it protects. See
README.md's "Capability receipts" section.
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


def validate(
    matrix: dict[str, Any],
    receipts_dir: Path,
    only: set[str] | None,
    allow_skip: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    required_ids = _required_capability_ids(matrix, only)
    if not required_ids:
        errors.append(
            "NO_REQUIRED_CAPABILITIES: nothing to validate -- either "
            "capabilities.yaml has no gating: true entries, or --capability-id "
            "filtered everything out"
        )
        return errors

    allow_skip = allow_skip or set()
    unknown_allow_skip = allow_skip - set(required_ids)
    if unknown_allow_skip:
        errors.append(
            f"BAD_ALLOW_SKIP: --allow-skip {sorted(unknown_allow_skip)} "
            "is not among the capability ids being validated"
        )

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
        status = receipt["status"]
        detail = f" ({receipt['detail']})" if receipt.get("detail") else ""
        if status == "failed":
            errors.append(f"FAILED: '{capability_id}' receipt says status='failed'{detail}")
        elif status == "skipped" and capability_id not in allow_skip:
            errors.append(
                f"UNEXPECTED_SKIP: '{capability_id}' receipt says "
                f"status='skipped'{detail}, but this id was not passed via "
                "--allow-skip -- a skip is not an accepted outcome for it"
            )
        # status == "passed", or status == "skipped" for an
        # explicitly-allowed id: satisfied.
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
    parser.add_argument(
        "--allow-skip",
        action="append",
        dest="allow_skip",
        default=None,
        help=(
            "accept a status: skipped receipt for this capability id (repeatable); "
            "any other gating id with status: skipped fails validation -- see this "
            "script's own module docstring for why skip-tolerance is opt-in per id"
        ),
    )
    args = parser.parse_args()

    with args.matrix.open(encoding="utf-8") as fh:
        matrix = yaml.safe_load(fh)
    if not isinstance(matrix, dict) or "capabilities" not in matrix:
        print(f"validate_capability_receipts: {args.matrix}: expected a 'capabilities' key", file=sys.stderr)
        return 1

    only = set(args.capability_ids) if args.capability_ids else None
    allow_skip = set(args.allow_skip) if args.allow_skip else None
    errors = validate(matrix, args.receipts_dir, only, allow_skip)

    if errors:
        print(f"validate_capability_receipts: {len(errors)} problem(s) found:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    required_ids = _required_capability_ids(matrix, only)
    print(
        f"validate_capability_receipts: OK -- {len(required_ids)} gating "
        f"capability receipt(s) satisfied"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
