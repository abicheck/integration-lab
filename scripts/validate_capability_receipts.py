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
  5. WRONG_JOB_PROVENANCE -- a receipt's own `workflow`/`job` fields don't
     match what capabilities.yaml itself declares for that capability id.
     A schema-valid, `status: passed` receipt with a mismatched job is
     exactly as untrustworthy as a missing one -- e.g. a receipt for
     `math-source-gate` claiming it came from some other job, or from a
     workflow other than `abi-scan.yml`.
  6. MISSING_PROVENANCE -- a receipt's `run_id`, `run_attempt`, or `sha`
     is empty (Codex review, fresh evidence: those fields are optional on
     capability_receipts.build_receipt, so nothing before this required a
     caller to actually populate them -- a receipt naming no run at all
     proved nothing about which run produced it).
  7. STALE_PROVENANCE -- only checked when `--expect-run-id`/
     `--expect-run-attempt`/`--expect-sha` are given: the receipt's own
     `run_id`/`run_attempt`/`sha` don't match the run this validation is
     running under. `run_attempt` matters independently of `run_id`
     (Codex review, fresh evidence): GitHub's own docs state `run_id`
     "does not change if you re-run the workflow run", only
     `run_attempt` does -- so pinning `run_id` alone would still accept a
     `status: passed` receipt left over from an earlier, different
     attempt of the SAME run whose actual producer step never ran (or
     failed) on the current attempt. Opt-in (not required) because a
     local, out-of-CI invocation of this script has no real run
     id/attempt/sha to pin against; both `abi-scan.yml`'s and
     `scenarios.yml`'s own validation steps pass `${{ github.run_id }}`/
     `${{ github.run_attempt }}`/`${{ github.sha }}` explicitly, so
     neither production call site skips this check.

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


def _required_capabilities(matrix: dict[str, Any], only: set[str] | None) -> list[dict[str, Any]]:
    """The full capabilities.yaml entries (not just ids) for every
    `gating: true` capability in scope -- callers need the entry's own
    `workflow`/`job` to cross-check a receipt's provenance against it."""
    entries = [
        entry
        for entry in matrix.get("capabilities", [])
        if entry.get("gating") is True and isinstance(entry.get("id"), str)
    ]
    if only is not None:
        ids = {e["id"] for e in entries}
        unknown = only - ids
        if unknown:
            raise SystemExit(
                f"validate_capability_receipts: --capability-id {sorted(unknown)} "
                "is not a gating: true entry in capabilities.yaml"
            )
        entries = [e for e in entries if e["id"] in only]
    return entries


def _required_capability_ids(matrix: dict[str, Any], only: set[str] | None) -> list[str]:
    return [e["id"] for e in _required_capabilities(matrix, only)]


def validate(
    matrix: dict[str, Any],
    receipts_dir: Path,
    only: set[str] | None,
    allow_skip: set[str] | None = None,
    expect_run_id: str | None = None,
    expect_run_attempt: str | None = None,
    expect_sha: str | None = None,
) -> list[str]:
    errors: list[str] = []
    required = _required_capabilities(matrix, only)
    if not required:
        errors.append(
            "NO_REQUIRED_CAPABILITIES: nothing to validate -- either "
            "capabilities.yaml has no gating: true entries, or --capability-id "
            "filtered everything out"
        )
        return errors

    required_ids = {e["id"] for e in required}
    allow_skip = allow_skip or set()
    unknown_allow_skip = allow_skip - required_ids
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

    for entry in required:
        capability_id = entry["id"]
        receipt = receipts.get(capability_id)
        if receipt is None:
            errors.append(
                f"MISSING_RECEIPT: '{capability_id}' is gating: true but no "
                f"receipt was found under {receipts_dir}"
            )
            continue

        # Provenance: a schema-valid, status: passed receipt still proves
        # nothing if it wasn't actually produced by the job capabilities.yaml
        # itself says answers this capability, for the run being judged
        # (Codex review, fresh evidence). Checked before the status itself,
        # since an untrustworthy receipt's status isn't worth reading.
        entry_workflow, entry_job = entry.get("workflow"), entry.get("job")
        if entry_workflow and receipt["workflow"] != entry_workflow:
            errors.append(
                f"WRONG_JOB_PROVENANCE: '{capability_id}' receipt claims "
                f"workflow={receipt['workflow']!r}, but capabilities.yaml "
                f"declares workflow={entry_workflow!r} for it"
            )
            continue
        if entry_job and receipt["job"] != entry_job:
            errors.append(
                f"WRONG_JOB_PROVENANCE: '{capability_id}' receipt claims "
                f"job={receipt['job']!r}, but capabilities.yaml declares "
                f"job={entry_job!r} for it"
            )
            continue
        if not receipt.get("run_id") or not receipt.get("run_attempt") or not receipt.get("sha"):
            errors.append(
                f"MISSING_PROVENANCE: '{capability_id}' receipt has no "
                "run_id/run_attempt/sha -- cannot confirm which run produced it"
            )
            continue
        if expect_run_id is not None and receipt["run_id"] != expect_run_id:
            errors.append(
                f"STALE_PROVENANCE: '{capability_id}' receipt has "
                f"run_id={receipt['run_id']!r}, expected {expect_run_id!r}"
            )
            continue
        # run_attempt checked independently of run_id: a re-run keeps the
        # same run_id but increments run_attempt (see the module docstring),
        # so pinning run_id alone would still accept a receipt left over
        # from a different attempt of the identical run.
        if expect_run_attempt is not None and receipt["run_attempt"] != expect_run_attempt:
            errors.append(
                f"STALE_PROVENANCE: '{capability_id}' receipt has "
                f"run_attempt={receipt['run_attempt']!r}, expected {expect_run_attempt!r}"
            )
            continue
        if expect_sha is not None and receipt["sha"] != expect_sha:
            errors.append(
                f"STALE_PROVENANCE: '{capability_id}' receipt has "
                f"sha={receipt['sha']!r}, expected {expect_sha!r}"
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
    parser.add_argument(
        "--expect-run-id",
        default=None,
        help="reject a receipt whose own run_id doesn't match this (e.g. ${{ github.run_id }})",
    )
    parser.add_argument(
        "--expect-run-attempt",
        default=None,
        help=(
            "reject a receipt whose own run_attempt doesn't match this "
            "(e.g. ${{ github.run_attempt }}) -- checked independently of "
            "--expect-run-id, since a re-run keeps the same run_id"
        ),
    )
    parser.add_argument(
        "--expect-sha",
        default=None,
        help="reject a receipt whose own sha doesn't match this (e.g. ${{ github.sha }})",
    )
    args = parser.parse_args()

    with args.matrix.open(encoding="utf-8") as fh:
        matrix = yaml.safe_load(fh)
    if not isinstance(matrix, dict) or "capabilities" not in matrix:
        print(f"validate_capability_receipts: {args.matrix}: expected a 'capabilities' key", file=sys.stderr)
        return 1

    only = set(args.capability_ids) if args.capability_ids else None
    allow_skip = set(args.allow_skip) if args.allow_skip else None
    errors = validate(
        matrix,
        args.receipts_dir,
        only,
        allow_skip,
        expect_run_id=args.expect_run_id,
        expect_run_attempt=args.expect_run_attempt,
        expect_sha=args.expect_sha,
    )

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
