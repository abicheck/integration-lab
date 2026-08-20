#!/usr/bin/env python3
"""Derive detection-correctness-scenarios-{castxml,clang} receipts
(scripts/capability_receipts.py) from scripts/run_scenario.py's own
scenario-results/summary.json.

Why a separate script rather than teaching run_scenario.py to write
receipts itself: run_scenario.py's job is the oracle (did abicheck detect
what scenarios/manifest.yaml says it should) and it already fails the CI
job on any mismatch -- correct and unchanged. This script answers a
narrower, distinct question for capabilities.yaml's own
detection-correctness-scenarios-castxml/-clang entries specifically: did
*that capability's own profile* (every result with
`profile == "castxml"`, respectively `"clang"`) actually get exercised
and pass, this run. Kept separate so run_scenario.py's own oracle logic
never has to know about capabilities.yaml's id scheme, and so a future
capability id mapping change doesn't need to touch the oracle at all.

A capability's status is:
  - "skipped" if summary.json has zero results for that profile (the
    manifest currently declares no per-profile scenario at all for it --
    not a failure, just nothing to check yet);
  - "passed" if every result for that profile has passed == true;
  - "failed" otherwise -- including when summary.json itself contains a
    malformed entry (CodeRabbit review, fresh evidence): a JSON string
    like `"passed": "false"` is truthy in Python, so the naive
    `all(r.get("passed") ...)` check would have emitted a PASSED receipt
    for a result that actually failed; a non-object list entry would have
    raised an unhandled AttributeError before any receipt was written at
    all. Both fail closed instead -- see derive_statuses()'s own
    docstring for exactly what counts as malformed and why a malformed
    entry anywhere fails BOTH capabilities rather than only the one
    profile it superficially names (an entry too corrupted to attribute
    safely can't be trusted to describe only that one profile either).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from capability_receipts import ReceiptError, build_receipt, write_receipt

REPO_ROOT = Path(__file__).resolve().parent.parent

# profile name (as written in scenarios/manifest.yaml's `expected:` map,
# and as run_scenario.py's own result["profile"] field) -> the
# capabilities.yaml id it proves execution for.
PROFILE_TO_CAPABILITY_ID = {
    "castxml": "detection-correctness-scenarios-castxml",
    "clang": "detection-correctness-scenarios-clang",
}


def derive_statuses(results: list) -> tuple[dict[str, str], list[str]]:
    """{capability_id: status} for every profile in PROFILE_TO_CAPABILITY_ID,
    plus a list of human-readable descriptions of any malformed entry found.

    Validates every entry defensively before trusting it (Codex/CodeRabbit
    review, fresh evidence -- see the module docstring): an entry that
    isn't a JSON object, or whose own `passed` field isn't a real JSON
    boolean, is malformed. A malformed entry anywhere in `results` fails
    BOTH capabilities (not just the one profile it superficially claims):
    a non-object entry can't even be attributed to a profile safely, and
    trusting the rest of a summary.json known to contain corrupted data
    would be exactly the "fail open on partial evidence" this whole
    receipt mechanism exists to avoid.
    """
    malformed: list[str] = []
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            malformed.append(f"result entry {i} is not a JSON object: {r!r}")
            continue
        profile = r.get("profile")
        if profile in PROFILE_TO_CAPABILITY_ID and not isinstance(r.get("passed"), bool):
            malformed.append(
                f"result entry {i} (profile={profile!r}) has a non-boolean "
                f"'passed' field: {r.get('passed')!r}"
            )

    if malformed:
        return {cap_id: "failed" for cap_id in PROFILE_TO_CAPABILITY_ID.values()}, malformed

    statuses: dict[str, str] = {}
    for profile, capability_id in PROFILE_TO_CAPABILITY_ID.items():
        profile_results = [r for r in results if r.get("profile") == profile]
        if not profile_results:
            statuses[capability_id] = "skipped"
        elif all(r["passed"] for r in profile_results):
            statuses[capability_id] = "passed"
        else:
            statuses[capability_id] = "failed"
    return statuses, []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        type=Path,
        default=REPO_ROOT / "scenario-results" / "summary.json",
        help="path to run_scenario.py's summary.json",
    )
    parser.add_argument("--receipts-dir", type=Path, required=True)
    parser.add_argument("--workflow", default="scenarios.yml")
    parser.add_argument("--job", default="scenarios")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-attempt", default="")
    parser.add_argument("--sha", default="")
    args = parser.parse_args()

    try:
        with args.summary.open(encoding="utf-8") as fh:
            results = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # No readable summary at all -- every profile receipt is "failed",
        # not silently absent: run_scenario.py itself would already have
        # failed the job in this case, but a receipt must say so too,
        # rather than leaving downstream validation to find no receipt and
        # guess why.
        print(f"emit_scenario_receipts: could not read {args.summary}: {exc}", file=sys.stderr)
        results = None

    if results is None:
        statuses = {cap_id: "failed" for cap_id in PROFILE_TO_CAPABILITY_ID.values()}
        detail = f"summary.json unreadable at {args.summary}"
    elif not isinstance(results, list):
        print(f"emit_scenario_receipts: {args.summary} did not contain a JSON list", file=sys.stderr)
        statuses = {cap_id: "failed" for cap_id in PROFILE_TO_CAPABILITY_ID.values()}
        detail = f"summary.json at {args.summary} was not a list"
    else:
        statuses, malformed = derive_statuses(results)
        detail = "; ".join(malformed)

    exit_code = 0
    for capability_id, status in statuses.items():
        try:
            receipt = build_receipt(
                capability_id=capability_id,
                status=status,
                workflow=args.workflow,
                job=args.job,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                sha=args.sha,
                detail=detail,
            )
            path = write_receipt(args.receipts_dir, receipt)
        except ReceiptError as exc:
            print(f"emit_scenario_receipts: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(f"emit_scenario_receipts: wrote {path} (status={status})")
        if status != "passed":
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
