"""Every coverage-bearing workflow must be inside the capability matrix's scope.

Codex found this on PR #30: `integration-shadow.yml` was outside
`IN_SCOPE_WORKFLOWS`, so none of its jobs had to be declared -- including
`scenarios_make` and `scenario_parity`, the cross-build coverage README
presents as validated. Deleting or renaming either job left
check_capability_matrix.py green while that coverage silently vanished.

The scope set is the whole load-bearing part of that checker: a workflow
omitted from it is a workflow whose jobs nothing has to declare. So the
omission itself is what gets pinned here, not just the entries.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_capability_matrix as matrix  # noqa: E402

#: Workflows deliberately outside the matrix, with the reason. Anything else
#: under .github/workflows must be in scope -- see the module docstring.
OUT_OF_SCOPE = {
    # Maintenance: these produce or publish baselines, they do not answer
    # "which ABI axis does this lab validate".
    "baseline.yml": "baseline maintenance, not validation",
    "profile-baseline.yml": "baseline maintenance, not validation",
    "project-baseline.yml": "baseline maintenance, not validation",
    "release.yml": "release-contract publication, not validation",
    # Measurement rather than correctness.
    "performance.yml": "perf trend measurement, not correctness",
    # Certifies a CANDIDATE scanner version on a schedule -- it answers "is
    # the upstream we are about to pin safe", not "what does this lab cover
    # on a PR". (scenarios-canary.yml IS in scope: it runs this repo's own
    # detection suite.)
    "canary.yml": "certifies a candidate scanner, not this lab's coverage",
    # The checker's own workflow. Putting it in scope would require the
    # matrix to declare the job that validates the matrix.
    "capability-matrix.yml": "runs check_capability_matrix.py itself",
}


def _workflows() -> set[str]:
    return {p.name for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml")}


def test_integration_shadow_is_in_scope():
    assert "integration-shadow.yml" in matrix.IN_SCOPE_WORKFLOWS


def test_every_workflow_is_either_in_scope_or_explicitly_excused():
    unaccounted = _workflows() - matrix.IN_SCOPE_WORKFLOWS - set(OUT_OF_SCOPE)
    assert not unaccounted, (
        f"{sorted(unaccounted)}: neither in the capability matrix's scope nor "
        "listed as deliberately excused. A workflow outside the scope set is "
        "one whose jobs nothing has to declare."
    )


def test_the_scope_set_names_only_real_workflows():
    assert not (matrix.IN_SCOPE_WORKFLOWS - _workflows())


@pytest.mark.parametrize("job", ["scenarios_make", "scenario_parity"])
def test_the_cross_build_coverage_jobs_are_declared(job):
    """The two jobs named in review, by job name rather than by entry id --
    an entry that stops pointing at them is the drift being guarded."""
    entries = yaml.safe_load(
        (REPO_ROOT / "capabilities.yaml").read_text(encoding="utf-8")
    )["capabilities"]
    declared = {
        entry["job"]
        for entry in entries
        if entry.get("workflow") == "integration-shadow.yml"
        and entry.get("status") in matrix.STATUSES_REQUIRING_JOB
    }
    assert job in declared
