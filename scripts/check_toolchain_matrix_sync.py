#!/usr/bin/env python3
"""Check that abi-scan.yml's `toolchain_matrix` job's `strategy.matrix.include`
list matches the `matrix_leg` fields capabilities.yaml's `toolchain-matrix-*`
entries declare.

Why this exists: phase 1/2 (see capabilities.yaml's own header comment and
scripts/check_capability_matrix.py/scripts/gen_capability_gaps.py) made this
lab's *coverage claims* machine-checked against the real workflow -- but the
`toolchain_matrix` job's actual compiler/standard legs (`cc`/`cxx`/`std`)
still lived only in abi-scan.yml's hardcoded `strategy.matrix.include`, with
capabilities.yaml's own `toolchain-matrix-*` entries only ever describing
*that* leg existed, not what compiler it actually runs. A leg's compiler
could be silently repointed in abi-scan.yml (e.g. clang-18 -> clang-19)
without capabilities.yaml -- the thing that's supposed to answer "which
toolchains does this lab's CI actually exercise" -- ever finding out.

Deliberately NOT a live GitHub Actions dynamic matrix (a prior job emitting
JSON consumed via `fromJson()` in `toolchain_matrix`'s own `strategy.matrix`)
-- that would restructure a real, already-working job in the one workflow
this repo protects most carefully (see README's "Gate / PR-comment
architecture" and CODEOWNERS), with no way to dry-run the result outside a
real Actions run. This script gets the same "capabilities.yaml can't drift
from what CI actually runs" property with much less risk: the workflow
YAML stays exactly as authored, and this check just proves the two
declarative sources -- capabilities.yaml's `matrix_leg` fields and
abi-scan.yml's `strategy.matrix.include` -- still agree, the same
"declarative table + one script that checks reality" shape phase 1 already
established for job references and phase 2 for the README gap list.

Comparison is a set of (cc, cxx, std) triples, not a positional list
comparison and not keyed by either file's own `id` spelling -- the two
files independently name each leg (capabilities.yaml:
`toolchain-matrix-gcc-cxx17`, abi-scan.yml: `gcc-cxx17`), and matrix job
order has no operational meaning (`fail-fast: false`, `matrix.include`
entries all run independently), so the only thing worth being strict about
is that the same set of real compiler/standard combinations exists on both
sides.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "check_toolchain_matrix_sync: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "capabilities.yaml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "abi-scan.yml"

_REQUIRED_LEG_KEYS = frozenset({"cc", "cxx", "std"})


def _capability_legs(matrix: dict[str, Any]) -> tuple[set[tuple[str, str, str]], list[str]]:
    """Returns (leg triples, errors) from every capabilities.yaml entry
    whose `job` is `toolchain_matrix`."""
    errors: list[str] = []
    legs: set[tuple[str, str, str]] = set()
    for entry in matrix.get("capabilities", []):
        if entry.get("job") != "toolchain_matrix":
            continue
        entry_id = entry.get("id", "<missing id>")
        leg = entry.get("matrix_leg")
        if not isinstance(leg, dict) or set(leg) != _REQUIRED_LEG_KEYS:
            errors.append(
                f"BAD_MATRIX_LEG: '{entry_id}' has job=toolchain_matrix but "
                f"matrix_leg={leg!r}, must be a mapping with exactly "
                f"{sorted(_REQUIRED_LEG_KEYS)}"
            )
            continue
        if not all(isinstance(v, str) and v for v in leg.values()):
            errors.append(
                f"BAD_MATRIX_LEG: '{entry_id}'.matrix_leg values must all be "
                f"non-empty strings, got {leg!r}"
            )
            continue
        legs.add((leg["cc"], leg["cxx"], leg["std"]))
    return legs, errors


def _workflow_legs(workflow: dict[str, Any]) -> tuple[set[tuple[str, str, str]], list[str]]:
    errors: list[str] = []
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    job = jobs.get("toolchain_matrix") if isinstance(jobs, dict) else None
    include = (
        job.get("strategy", {}).get("matrix", {}).get("include")
        if isinstance(job, dict)
        else None
    )
    if not isinstance(include, list) or not include:
        errors.append(
            f"{WORKFLOW_PATH}: jobs.toolchain_matrix.strategy.matrix.include "
            "is missing or empty"
        )
        return set(), errors

    legs: set[tuple[str, str, str]] = set()
    for i, leg in enumerate(include):
        if not isinstance(leg, dict) or not _REQUIRED_LEG_KEYS <= set(leg):
            errors.append(
                f"{WORKFLOW_PATH}: strategy.matrix.include[{i}]={leg!r} is "
                f"missing one of {sorted(_REQUIRED_LEG_KEYS)}"
            )
            continue
        legs.add((leg["cc"], leg["cxx"], leg["std"]))
    return legs, errors


def check(matrix: dict[str, Any], workflow: dict[str, Any]) -> list[str]:
    cap_legs, errors = _capability_legs(matrix)
    wf_legs, wf_errors = _workflow_legs(workflow)
    errors.extend(wf_errors)
    if errors:
        return errors

    only_in_capabilities = cap_legs - wf_legs
    only_in_workflow = wf_legs - cap_legs
    if only_in_capabilities:
        errors.append(
            "STALE_MATRIX_LEG: capabilities.yaml declares "
            f"{sorted(only_in_capabilities)} but abi-scan.yml's "
            "toolchain_matrix no longer runs it"
        )
    if only_in_workflow:
        errors.append(
            "UNDOCUMENTED_MATRIX_LEG: abi-scan.yml's toolchain_matrix runs "
            f"{sorted(only_in_workflow)} but no capabilities.yaml entry's "
            "matrix_leg names it"
        )
    return errors


def main() -> int:
    with MATRIX_PATH.open() as f:
        matrix = yaml.safe_load(f)
    with WORKFLOW_PATH.open() as f:
        workflow = yaml.safe_load(f)

    errors = check(matrix, workflow)
    if errors:
        print(
            f"check_toolchain_matrix_sync: {len(errors)} problem(s) found:\n",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("check_toolchain_matrix_sync: OK -- capabilities.yaml matches abi-scan.yml's toolchain_matrix legs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
