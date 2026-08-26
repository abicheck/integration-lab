"""No workflow may interpolate contributor-controlled text into a shell script.

Codex review (P1), on the `demo_oracle` job: GitHub substitutes a
`${{ ... }}` expression into a `run:` block BEFORE the shell or any
interpreter sees it, so the substituted text is script, not data. Git permits
branch names like `foo$(id)`, and on a `pull_request` from a fork the branch
name is chosen by the contributor -- so the substitution executes on the
runner. Sitting inside a quoted string is no defence: the same substitution
can close the quote.

The fix in every case is the same -- pass the value through `env:` and read it
from the environment, where it stays data at every layer. This test pins the
class rather than the one instance, because the hole is easy to reintroduce
and invisible in review: the dangerous version looks exactly like the safe
one.

Only contexts an outside contributor can actually influence are listed.
`github.repository`, `github.sha`, `runner.temp` and this repo's own step
outputs are not attacker-controlled and are deliberately not flagged -- a
guard that fires on those would be turned off.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))

#: Expression contexts whose value an outside contributor can set on a fork
#: pull request (branch name, PR title/body, issue and comment bodies).
ATTACKER_CONTROLLED = re.compile(
    r"\$\{\{\s*github\.(?:"
    r"head_ref"
    r"|event\.pull_request\.(?:title|body|head\.ref|head\.label)"
    r"|event\.issue\.(?:title|body)"
    r"|event\.comment\.body"
    r"|event\.review\.body"
    r")\b",
)


def _steps(document: dict) -> list[tuple[str, str, dict]]:
    out = []
    for job_id, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                out.append((job_id, step.get("name") or step.get("uses") or "<unnamed>", step))
    return out


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_attacker_controlled_expression_reaches_a_run_block(workflow: Path):
    document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    offenders = []
    for job_id, step_name, step in _steps(document):
        script = step.get("run")
        if not isinstance(script, str):
            continue
        if ATTACKER_CONTROLLED.search(script):
            offenders.append(f"{workflow.name}:{job_id}:{step_name}")
    assert not offenders, (
        "contributor-controlled expression interpolated directly into a run: block in "
        + ", ".join(sorted(offenders))
        + " -- pass it through `env:` and read it from the environment instead"
    )


def test_the_demo_oracle_reads_the_branch_from_the_environment():
    """The specific job the finding was raised against."""
    document = yaml.safe_load((WORKFLOWS[0].parent / "abi-scan.yml").read_text(encoding="utf-8"))
    job = document["jobs"]["demo_oracle"]
    consumers = [s for s in job["steps"] if isinstance(s.get("run"), str) and "HEAD_REF" in s["run"]]
    assert consumers, "no demo_oracle step reads HEAD_REF"
    for step in consumers:
        assert (step.get("env") or {}).get("HEAD_REF") == "${{ github.head_ref }}", step.get("name")
        assert "${{ github.head_ref }}" not in step["run"], step.get("name")


def test_the_guard_would_actually_catch_the_original_bug():
    """A guard nobody has seen fail is not known to work."""
    original = '''python3 -c "
    import sys, yaml
    sys.exit(0 if '${{ github.head_ref }}' in branches else 1)
    "'''
    assert ATTACKER_CONTROLLED.search(original)


@pytest.mark.parametrize("safe", [
    'echo "${{ github.repository }}"',
    'echo "${{ github.sha }}"',
    'echo "${{ steps.demo.outputs.is_demo }}"',
    'echo "${{ runner.temp }}"',
    'echo "$HEAD_REF"',
])
def test_the_guard_does_not_fire_on_values_a_contributor_cannot_set(safe: str):
    assert not ATTACKER_CONTROLLED.search(safe)
