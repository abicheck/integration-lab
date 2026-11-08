"""Fork-PR comment wiring (abicheck#1945): producer and publisher agree.

abi-scan.yml's gate step must route a read-only (fork/Dependabot) run to the
artifact path, and abi-report-pr.yml must be the trusted `workflow_run`
publisher for exactly that workflow, with the reusable workflow's three
permissions and no contributor checkout.
"""
from __future__ import annotations

from pathlib import Path

import yaml

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text(encoding="utf-8"))


def _gate_step() -> dict:
    steps = _load("abi-scan.yml")["jobs"]["scan"]["steps"]
    return next(s for s in steps if s.get("id") == "scan")


def test_gate_step_uses_auto_delivery() -> None:
    w = _gate_step()["with"]
    assert w["pr-comment"] is True
    assert w["pr-comment-via"] == "auto"
    # Unkeyed producer -> `abicheck-pr-report`, which report-pr.yml derives
    # from an empty comment-key.
    assert "pr-comment-key" not in w and "pr-comment-artifact-name" not in w


def test_only_the_gate_step_comments() -> None:
    for job in _load("abi-scan.yml")["jobs"].values():
        for step in job.get("steps", []):
            via = (step.get("with") or {}).get("pr-comment-via")
            if via is not None:
                assert step.get("id") == "scan"


def test_publisher_triggers_on_the_producer_by_its_name() -> None:
    producer = _load("abi-scan.yml")
    pub = _load("abi-report-pr.yml")
    trigger = pub[True]["workflow_run"]  # PyYAML reads `on:` as True
    assert trigger["workflows"] == [producer["name"]]
    assert trigger["types"] == ["completed"]


def test_publisher_permissions_and_reusable_call() -> None:
    pub = _load("abi-report-pr.yml")
    assert pub["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "write",
    }
    (job,) = pub["jobs"].values()
    assert job["uses"].startswith(
        "abicheck/abicheck/.github/workflows/report-pr.yml@"
    )
    assert "steps" not in job  # no checkout, nothing of the PR's executes
    w = job["with"]
    assert w["expect-workflow"] == ".github/workflows/abi-scan.yml"
    gate = _gate_step()["with"]
    assert w["gate-breaking"] is gate["fail-on-breaking"]
    assert w["gate-api-break"] is gate["fail-on-api-break"]
