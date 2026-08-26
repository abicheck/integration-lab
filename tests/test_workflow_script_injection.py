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


# --- identity, not just name (Codex review) -----------------------------


def test_demo_oracle_requires_the_head_repo_to_be_this_repository():
    """A branch NAME does not identify a demonstration.

    A fork PR whose contributor happens to name their branch
    `test/compatible-addition` would match the manifest, and this gating
    oracle would then hold their unrelated change to that demo's declared
    verdict. The demonstration branches live in this repository, so the head
    repo has to match too.
    """
    document = yaml.safe_load((WORKFLOWS[0].parent / "abi-scan.yml").read_text(encoding="utf-8"))
    step = next(
        s for s in document["jobs"]["demo_oracle"]["steps"]
        if s.get("name") == "Is this a demonstration branch?"
    )
    assert step["env"]["HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    script = step["run"]
    assert "$HEAD_REPO" in script and "github.repository" in script
    # The repo check must come before the manifest lookup, so a fork PR never
    # even reads the demonstration set.
    assert script.index("HEAD_REPO") < script.index("demos/manifest.yaml")


#: Every job that reads `select`'s matrix. A bare `always()` on any of these
#: turns a CANCELLED select -- what happens to every superseded run when a new
#: commit is pushed -- into "select job produced no matrix" and a red check,
#: making a routine supersession indistinguishable from a real failure. Both
#: were fixed together after fixing only one proved to be half a fix.
MATRIX_CONSUMERS = ("cross_build_equivalence", "integration_gate")


@pytest.mark.parametrize("job_id", MATRIX_CONSUMERS)
def test_a_cancelled_select_does_not_produce_a_red_check(job_id: str):
    document = yaml.safe_load(
        (WORKFLOWS[0].parent / "integration-shadow.yml").read_text(encoding="utf-8")
    )
    condition = document["jobs"][job_id]["if"]
    assert "needs.select.result != 'cancelled'" in condition, condition


@pytest.mark.parametrize("job_id", MATRIX_CONSUMERS)
def test_a_genuinely_failed_select_still_reports(job_id: str):
    """`!= 'cancelled'`, not `== 'success'`.

    Cancellation is not a result, so stepping aside for it loses nothing. A
    genuinely failed select is different: a *skipped* required check can leave
    a PR looking mergeable while select is broken, so these must still run and
    go red on a real failure.
    """
    document = yaml.safe_load(
        (WORKFLOWS[0].parent / "integration-shadow.yml").read_text(encoding="utf-8")
    )
    condition = document["jobs"][job_id]["if"]
    assert "== 'success'" not in condition, condition


def test_every_job_reading_the_matrix_is_guarded():
    """Catches a future job added with a bare always() and the same hole."""
    document = yaml.safe_load(
        (WORKFLOWS[0].parent / "integration-shadow.yml").read_text(encoding="utf-8")
    )
    unguarded = []
    for job_id, job in document["jobs"].items():
        if "select" not in (job.get("needs") or []):
            continue
        condition = str(job.get("if") or "")
        # `build` guards by reading the matrix output directly, which is
        # equally safe -- an empty matrix skips it.
        if "needs.select.outputs.matrix" in condition:
            continue
        if "needs.select.result" not in condition:
            unguarded.append(job_id)
    assert not unguarded, (
        "these jobs consume `select` but do not guard on its result, so a "
        "cancelled select will fail them: " + ", ".join(sorted(unguarded))
    )


def test_every_yaml_consuming_job_installs_pyyaml():
    """Codex review: demo_oracle read demos/manifest.yaml with `import yaml`
    but nothing installed PyYAML -- the only YAML-consuming job in the
    repository not to. It is GATING, so on a runner image without the module
    it would turn every same-repository PR red rather than only demonstration
    branches. Pinned as a class: an undeclared interpreter dependency is
    invisible until the image changes under you.
    """
    offenders = []
    for workflow in WORKFLOWS:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job_id, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            scripts = [
                step["run"] for step in (job.get("steps") or [])
                if isinstance(step, dict) and isinstance(step.get("run"), str)
            ]
            blob = "\n".join(scripts)
            if "import yaml" not in blob and "yaml.safe_load" not in blob:
                continue
            if "pyyaml" not in blob.lower():
                offenders.append(f"{workflow.name}:{job_id}")
    assert not offenders, (
        "these jobs import yaml without installing PyYAML: " + ", ".join(sorted(offenders))
    )


# --- the judged branch must not supply the judgement (Codex review) ------


def test_demo_oracle_reads_the_manifest_and_script_from_the_base():
    """This job detects drift in a generated demonstration branch, so it must
    not read its expectations out of the branch it is judging.

    A branch that dropped its own entry from demos/manifest.yaml would set
    is_demo=false and skip the oracle; one that rewrote its `expect` block
    would be checked against its own rewritten expectation. Pinning the
    checkout to the base covers the oracle SCRIPT too -- a branch that could
    rewrite check_demo_oracle.py to return 0 defeats this just as completely.
    """
    document = yaml.safe_load((WORKFLOWS[0].parent / "abi-scan.yml").read_text(encoding="utf-8"))
    steps = document["jobs"]["demo_oracle"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert len(checkouts) == 1, "one checkout, so there is no head copy to read by accident"
    ref = (checkouts[0].get("with") or {}).get("ref")
    assert ref == "${{ github.event.pull_request.base.sha }}", (
        "demo_oracle must check out the PR BASE, not the head -- otherwise the "
        f"branch being judged supplies its own expectations (ref={ref!r})"
    )


def test_demo_oracle_survives_a_base_with_no_manifest():
    """True during the transition, before the manifest reaches the default
    branch. A gating job must not crash on that."""
    document = yaml.safe_load((WORKFLOWS[0].parent / "abi-scan.yml").read_text(encoding="utf-8"))
    step = next(
        s for s in document["jobs"]["demo_oracle"]["steps"]
        if s.get("name") == "Is this a demonstration branch?"
    )
    script = step["run"]
    assert "[ ! -f demos/manifest.yaml ]" in script
    # The guard must precede the read, or it guards nothing.
    assert script.index("! -f demos/manifest.yaml") < script.index("yaml.safe_load")


def test_demo_oracle_tolerates_an_empty_demonstrations_list():
    document = yaml.safe_load((WORKFLOWS[0].parent / "abi-scan.yml").read_text(encoding="utf-8"))
    step = next(
        s for s in document["jobs"]["demo_oracle"]["steps"]
        if s.get("name") == "Is this a demonstration branch?"
    )
    assert "doc.get('demonstrations') or []" in step["run"]


# --- cancelled upstreams must not read as failures (three instances) -----


def _always_jobs_consuming_artifacts_hard():
    """Jobs that run on `always()` and download an upstream artifact WITHOUT
    continue-on-error -- i.e. that fail hard when the artifact is absent."""
    out = []
    for workflow in WORKFLOWS:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        for job_id, job in (document.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            condition = str(job.get("if") or "")
            if "always()" not in condition:
                continue
            hard = [
                step for step in (job.get("steps") or [])
                if isinstance(step, dict)
                and "actions/download-artifact" in str(step.get("uses", ""))
                and not step.get("continue-on-error")
            ]
            if hard:
                out.append((workflow.name, job_id, condition))
    return out


def test_every_always_job_consuming_an_artifact_guards_against_cancellation():
    """The general rule behind three separate failures.

    A cancelled upstream -- what happens to every superseded run -- uploads no
    artifacts. An `always()` job that then downloads one without
    continue-on-error fails hard, and a routine supersession is
    indistinguishable from a real defect. It cost three misreads via
    cross_build_equivalence and integration_gate, then a MISSING_RECEIPT
    failure in verify_capability_receipts.

    Stated as the general property because the earlier, narrower version of
    this guard covered only integration-shadow.yml's `select` consumers and
    missed both abi-scan.yml cases -- including one in a job added in this
    same PR, which this rule caught BEFORE it ever failed.

    A job that downloads with continue-on-error is deliberately degrading and
    is not covered: absence there is a handled outcome, not a hard failure.
    """
    unguarded = [
        f"{workflow}:{job_id}"
        for workflow, job_id, condition in _always_jobs_consuming_artifacts_hard()
        if "result !=" not in condition and "result ==" not in condition
    ]
    assert not unguarded, (
        "these jobs run on always() and download an upstream artifact without "
        "continue-on-error, so a CANCELLED upstream fails them: "
        + ", ".join(sorted(unguarded))
        + " -- add `&& needs.<upstream>.result != 'cancelled'`"
    )


def test_the_rule_actually_matches_the_known_jobs():
    """A rule that matches nothing would pass vacuously forever."""
    matched = {job_id for _, job_id, _ in _always_jobs_consuming_artifacts_hard()}
    assert {"verify_capability_receipts", "demo_oracle"} <= matched, matched
