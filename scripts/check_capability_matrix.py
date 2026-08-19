#!/usr/bin/env python3
"""Check capabilities.yaml against reality: every `covered`/`non_gating_watch`
entry's `job`/`workflow` must actually exist, and every job in an in-scope
workflow must be named by at least one entry.

Why this exists: capabilities.yaml is the machine-readable answer to "what
does this lab's CI actually validate" -- replacing the hand-typed prose that
used to live only in README.md's "Known limitations / follow-ups" section,
with nothing keeping it honest as jobs were added, renamed, or removed. This
script is that keeper, the same role scripts/check_coverage_contract.py plays
for one scan's own evidence completeness, and scripts/run_scenario.py plays
for the scenario oracle: read a declarative table, check it against the real
artifact it describes, fail closed on any disagreement.

Two independent failure modes, both real drift, both caught here:
  1. A capabilities.yaml entry claims a job/workflow that doesn't exist (a
     typo, or the referenced job was renamed/removed and the entry never
     updated) -- STALE_ENTRY.
  2. A job exists in an in-scope workflow but no capabilities.yaml entry
     names it -- UNDOCUMENTED_JOB. This is what actually prevents new
     coverage (or new duplication) from landing invisibly: add a job to
     abi-scan.yml without touching capabilities.yaml, and this script fails
     the PR instead of the gap only ever being noticed by someone reading
     the workflow file by hand.

Deliberately scoped to the workflows named in capabilities.yaml's own header
comment (abi-scan.yml, scenarios.yml, scenarios-canary.yml) -- baseline.yml
(baseline maintenance) and performance.yml (perf trend measurement) answer no
coverage question this matrix tracks, so they're never scanned for
"undocumented" jobs and a capabilities.yaml entry may not reference them
either (out-of-scope workflow reference is its own error, WORKFLOW_OUT_OF_SCOPE).
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
        "check_capability_matrix: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "capabilities.yaml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The only workflows this matrix is responsible for describing -- see the
# module docstring for why baseline.yml/performance.yml are deliberately
# excluded rather than omitted by oversight.
IN_SCOPE_WORKFLOWS = frozenset({"abi-scan.yml", "scenarios.yml", "scenarios-canary.yml"})

ALLOWED_STATUSES = frozenset({"covered", "non_gating_watch", "gap", "planned"})
# Statuses that must reference a real job -- the inverse (gap/planned) must
# NOT reference one, since there is nothing yet to check it against.
STATUSES_REQUIRING_JOB = frozenset({"covered", "non_gating_watch"})


def _load_matrix(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "capabilities" not in data or "dimensions" not in data:
        raise SystemExit(
            f"{path}: expected top-level 'dimensions' and 'capabilities' keys"
        )
    return data


def _workflow_job_names(path: Path) -> set[str]:
    with path.open() as f:
        data = yaml.safe_load(f)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return set()
    return set(jobs.keys())


def _real_jobs_by_workflow() -> dict[str, set[str]]:
    """{workflow filename: {job ids}} for every in-scope workflow that
    actually exists on disk. A workflow named in IN_SCOPE_WORKFLOWS but
    missing from .github/workflows/ is its own error, surfaced by the
    caller rather than silently returning an empty job set for it."""
    result: dict[str, set[str]] = {}
    for name in IN_SCOPE_WORKFLOWS:
        path = WORKFLOWS_DIR / name
        if path.is_file():
            result[name] = _workflow_job_names(path)
    return result


def check(matrix: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    dimensions = matrix["dimensions"]
    capabilities = matrix["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        errors.append("capabilities.yaml: 'capabilities' must be a non-empty list")
        return errors

    real_jobs = _real_jobs_by_workflow()
    for name in IN_SCOPE_WORKFLOWS:
        if name not in real_jobs:
            errors.append(
                f"MISSING_WORKFLOW: {name} is listed as in-scope but "
                f".github/workflows/{name} does not exist"
            )

    seen_ids: set[str] = set()
    # (workflow, job) pairs actually referenced by a capabilities.yaml entry
    # with a status that requires one -- used to compute UNDOCUMENTED_JOB.
    referenced: set[tuple[str, str]] = set()

    for entry in capabilities:
        entry_id = entry.get("id", "<missing id>")
        if entry_id in seen_ids:
            errors.append(f"DUPLICATE_ID: '{entry_id}' appears more than once")
        seen_ids.add(entry_id)

        status = entry.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(
                f"BAD_STATUS: '{entry_id}' has status={status!r}, "
                f"must be one of {sorted(ALLOWED_STATUSES)}"
            )
            continue

        workflow = entry.get("workflow")
        job = entry.get("job")

        if status in STATUSES_REQUIRING_JOB:
            if not workflow or not job:
                errors.append(
                    f"MISSING_JOB_REF: '{entry_id}' has status={status!r} "
                    "but no workflow/job to check it against"
                )
                continue
            if workflow not in IN_SCOPE_WORKFLOWS:
                errors.append(
                    f"WORKFLOW_OUT_OF_SCOPE: '{entry_id}' references "
                    f"workflow={workflow!r}, which is not one of "
                    f"{sorted(IN_SCOPE_WORKFLOWS)}"
                )
                continue
            if workflow not in real_jobs:
                # Already reported as MISSING_WORKFLOW above; don't double-report.
                continue
            if job not in real_jobs[workflow]:
                errors.append(
                    f"STALE_ENTRY: '{entry_id}' references "
                    f"{workflow}:{job}, but that job does not exist "
                    f"(real jobs: {sorted(real_jobs[workflow])})"
                )
                continue
            referenced.add((workflow, job))
        else:
            # gap/planned: must NOT claim a job -- there is nothing to check.
            if workflow or job:
                errors.append(
                    f"UNEXPECTED_JOB_REF: '{entry_id}' has status={status!r} "
                    "but names a workflow/job -- gaps have nothing to point at"
                )

        entry_dims = entry.get("dimensions", {})
        if not isinstance(entry_dims, dict):
            errors.append(f"BAD_DIMENSIONS: '{entry_id}' dimensions must be a mapping")
            continue
        # Codex review, PR #15: every declared top-level dimension must be
        # present on every entry (using the 'n/a' enum value when a
        # dimension genuinely doesn't apply) -- a *missing* key previously
        # passed silently, letting an entry advertise an incomplete axis
        # combination in what's supposed to be the exhaustive source of
        # truth. Checked against the FULL dimension set, not just the keys
        # the entry happens to declare.
        missing = sorted(set(dimensions) - set(entry_dims))
        if missing:
            errors.append(
                f"MISSING_DIMENSION: '{entry_id}' is missing "
                f"{missing} -- every entry must state a value (or 'n/a') "
                "for every declared dimension"
            )
        for dim_name, dim_value in entry_dims.items():
            allowed_values = dimensions.get(dim_name)
            if allowed_values is None:
                errors.append(
                    f"UNKNOWN_DIMENSION: '{entry_id}' uses dimension "
                    f"{dim_name!r}, not declared in the top-level 'dimensions' block"
                )
                continue
            if dim_value not in allowed_values:
                errors.append(
                    f"BAD_DIMENSION_VALUE: '{entry_id}'.{dim_name}={dim_value!r} "
                    f"not in {allowed_values}"
                )

    for workflow, job_names in real_jobs.items():
        for job in job_names:
            if (workflow, job) not in referenced:
                errors.append(
                    f"UNDOCUMENTED_JOB: {workflow}:{job} exists but no "
                    "capabilities.yaml entry (with status covered/"
                    "non_gating_watch) references it"
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
    args = parser.parse_args()

    matrix = _load_matrix(args.matrix)
    errors = check(matrix)

    if errors:
        print(f"check_capability_matrix: {len(errors)} problem(s) found:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    n = len(matrix["capabilities"])
    print(f"check_capability_matrix: OK -- {n} capability entries checked against reality")
    return 0


if __name__ == "__main__":
    sys.exit(main())
