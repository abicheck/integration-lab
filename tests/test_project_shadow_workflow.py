"""Structural guards on the native project shadow's baseline lifecycle.

The lifecycle these assert is the one thing PR #29 exists to make work:
every PR must resolve an identity-matched accepted-main baseline, receipt
what it resolved, and reach the real `check-project.yml` job.  Each
assertion here corresponds to a way that has already silently broken --
a restore that failed with no diagnosis, a `needs:` that skipped the
project job, a rebuild that could be mistaken for a cache hit.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "project-shadow.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def restore(workflow: dict) -> dict:
    return workflow["jobs"]["restore-baseline"]


def _steps(job: dict) -> list:
    return job["steps"]


def _named(job: dict, fragment: str) -> dict:
    for step in _steps(job):
        if fragment in (step.get("name") or ""):
            return step
    raise AssertionError(f"no step named like {fragment!r}")


def test_baseline_ref_resolves_through_the_shared_module(workflow: dict) -> None:
    """Ancestry classification lives in one tested place, not inline bash."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ci/baseline_resolution.py resolve" in text
    # The inline `git diff` walk this replaced had no --no-renames and no
    # receipt; it must not come back.
    assert "git diff --name-only" not in text


def test_restore_job_can_read_the_cache_listing(restore: dict) -> None:
    assert restore["permissions"]["actions"] == "read"
    assert restore["permissions"]["contents"] == "read"


def test_prefix_restore_is_allowed_only_as_transport(restore: dict) -> None:
    """restore-keys may match anything; identity still has to be checked."""
    step = _named(restore, "Restore accepted-main baseline")
    assert "restore-keys" in step["with"]
    receipt = _named(restore, "Require a receipted, identity-matched baseline")
    assert "--expected-generation 1" in receipt["run"]
    assert '--selected "$SOURCE_COMMIT"' in receipt["run"]
    assert '--profile "$PROFILE"' in receipt["run"]
    # No --no-fail on the second pass: this one is the gate.
    assert "--no-fail" not in receipt["run"]


def test_first_classification_never_gates(restore: dict) -> None:
    """The first pass only decides whether to rebuild."""
    assert "--no-fail" in _named(restore, "Classify the restored baseline")["run"]


def test_every_rebuild_step_is_gated_on_the_classification(restore: dict) -> None:
    condition = "steps.classify.outputs.usable != 'true'"
    rebuild_markers = (
        "Check out the accepted-main source commit",
        "Install build toolchain",
        "Rebuild the accepted-main build-output",
        "Check out abicheck at the reviewed pin",
        "Derive baseline libraries from build-output.json",
        "Rebuild the accepted-main baseline-set",
    )
    for marker in rebuild_markers:
        assert _named(restore, marker)["if"] == condition, marker


def test_rebuild_uses_the_source_commits_own_recipe(restore: dict) -> None:
    """A baseline built by the candidate's scripts is not a baseline."""
    step = _named(restore, "Rebuild the accepted-main build-output")
    assert step["working-directory"] == "accepted-main-src"
    build = _named(restore, "Rebuild the accepted-main baseline-set")
    assert build["with"]["project-ref"] == "${{ needs.baseline-ref.outputs.sha }}"
    assert build["with"]["baseline-generation"] == "1"
    assert build["with"]["validation"] == "strict"


def test_stale_prefix_match_cannot_contaminate_a_rebuild(restore: dict) -> None:
    assert "rm -rf baseline-set" in _named(
        restore, "Check out the accepted-main source commit"
    )["run"]


def test_receipt_is_published_even_when_the_job_fails(restore: dict) -> None:
    step = _named(restore, "Publish the baseline resolution receipt")
    assert step["if"] == "always()"
    assert step["with"]["name"] == "baseline-resolution-${{ matrix.profile }}"


def test_baseline_artifact_matches_the_upstream_convention(restore: dict) -> None:
    """check-project.yml downloads <prefix><profile>-<channel>."""
    step = _steps(restore)[-1]
    assert step["with"]["name"] == "abicheck-baseline-${{ matrix.profile }}-accepted-main"
    assert step["with"]["if-no-files-found"] == "error"


def test_project_job_waits_for_a_resolved_baseline(workflow: dict) -> None:
    project = workflow["jobs"]["project"]
    assert set(project["needs"]) == {"build", "restore-baseline", "baseline-ref"}


def test_plan_oracle_runs_the_run_plan_and_the_negative_aggregate(workflow: dict) -> None:
    text = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["plan-oracle"]["steps"]
    )
    assert "ci/validate_run_plan.py" in text
    assert "ci/validate_aggregate_fail_closed.py" in text
