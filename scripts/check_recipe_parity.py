#!/usr/bin/env python3
"""Check that no `abicheck/abicheck` Action step in this repo's workflows
passes a contract-defining CLI flag through `extra-args` -- a static guard
against exactly the bug that produced the recurring NOT_COMPARABLE /
`include_sequence` mismatch between the committed `math` baseline
(baseline.yml, `mode: dump`) and the canonical PR scan (abi-scan.yml,
`mode: scan`).

Root cause (see abi-scan.yml's own comment on its `scan` step for the
full history): the scan step passed `--public-header-dir include` through
raw `extra-args`, while a *typed* `public-header-dir:` input already
existed on the pinned Action and -- for `mode: scan` specifically --
forwards the value through an extra, candidate-sided `-H new=...` header
root that raw `extra-args` never gets (action.yml's own doc for this
input explains why: keeping a fresh `dump` baseline and a `scan --against`
it comparable on `include_sequence` instead of drifting apart for no real
recipe difference). baseline.yml's `dump` steps already used the typed
input; abi-scan.yml's `scan` steps didn't, so the two sides of one
logical comparison were extracted through two different effective
recipes even though both workflows *looked* like they were doing the same
thing -- prose ("this is the same recipe") is not a contract, only a
check like this one is.

This check is deliberately narrow and mechanical: it does not attempt to
model the Action's full input surface or actually resolve an effective
analysis recipe (that belongs in abicheck core -- see abicheck's own
AGENTS.md "Known gaps" for the extent of that work). It only asks: for
every `abicheck/abicheck` step in a workflow, does `extra-args` contain a
flag that a *typed* input on the same step already exists for? If a step
sets a typed input for a contract-defining flag, `extra-args` re-stating
that same flag is redundant at best (and was actively wrong for
`public-header-dir` on `scan`, per the divergence above) -- so the
combination is rejected outright, matching the docs (`README.md` /
`docs/` -- see the P0 recommendation this script implements) that
`extra-args` is for temporary experimentation, not for contract-defining
flags a typed input already covers.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "check_recipe_parity: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Every workflow that invokes the `abicheck/abicheck` composite Action.
# Listed explicitly (rather than globbed) so a new workflow adding an
# `abicheck/abicheck` step is a deliberate addition to this list, not a
# silent gap the way a glob could hide one under (e.g.) a workflow this
# repo doesn't consider a "canonical gate".
CHECKED_WORKFLOWS = (
    WORKFLOWS_DIR / "abi-scan.yml",
    WORKFLOWS_DIR / "baseline.yml",
    WORKFLOWS_DIR / "profile-baseline.yml",
    WORKFLOWS_DIR / "scenarios.yml",
)

# Maps a typed Action input name to the CLI flag spelling(s) it controls.
# Mirrors action.yml's own `INPUT_*` -> flag wiring (see action.yml's
# `## Header inputs` / `## Include directories` sections and its run
# script) -- kept as a static table here rather than parsed out of
# action.yml itself, since action.yml lives in a different repository and
# this check's job is to catch a *local* workflow mistake, not to track
# the Action's own input surface.
_TYPED_INPUT_TO_FLAGS: dict[str, tuple[str, ...]] = {
    "header": ("-H", "--header"),
    "old-header": ("--old-header",),
    "new-header": ("--new-header",),
    "public-header-dir": ("--public-header-dir",),
    "build-target": ("--build-target",),
    "include": ("-I", "--include"),
    "old-include": ("--old-include",),
    "new-include": ("--new-include",),
    "sources": ("--sources",),
    "build-info": ("--build-info",),
    "depth": ("--depth",),
    "ast-frontend": ("--ast-frontend",),
    "compiler": ("--compiler",),
    "policy": ("--policy",),
    "suppress": ("--suppress",),
}

# Reverse index: CLI flag -> typed input name, used to scan extra-args
# tokens for a flag that a typed input already exists for.
_FLAG_TO_TYPED_INPUT: dict[str, str] = {
    flag: typed_input
    for typed_input, flags in _TYPED_INPUT_TO_FLAGS.items()
    for flag in flags
}

_ABICHECK_USES_RE = re.compile(r"^abicheck/abicheck(?:@|$)")


def _abicheck_steps(workflow: dict[str, Any], path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Returns (step-label, `with:` dict) for every `abicheck/abicheck`
    step in every job, across both a plain `steps:` list and a
    `strategy.matrix`-driven job (same shape either way -- the matrix
    only changes how many times the step runs, not what its `with:`
    block says)."""
    steps: list[tuple[str, dict[str, Any]]] = []
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return steps
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for i, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if not isinstance(uses, str) or not _ABICHECK_USES_RE.match(uses):
                continue
            with_block = step.get("with")
            if not isinstance(with_block, dict):
                with_block = {}
            label = f"{path.name}:jobs.{job_id}.steps[{i}]"
            if step.get("id"):
                label += f" (id={step['id']!r})"
            elif step.get("name"):
                label += f" ({step['name']!r})"
            steps.append((label, with_block))
    return steps


def _extra_args_tokens(with_block: dict[str, Any]) -> list[str]:
    raw = with_block.get("extra-args")
    if not isinstance(raw, str) or not raw.strip():
        return []
    # A GitHub Actions expression (`${{ ... }}`) can't be tokenized
    # statically -- skip rather than mis-parse; this check only reasons
    # about literal, checked-in extra-args strings.
    if "${{" in raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return []


def check_step(label: str, with_block: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tokens = _extra_args_tokens(with_block)
    typed_inputs_present = {k for k, v in with_block.items() if k != "extra-args" and v not in (None, "")}

    shadowed: set[str] = set()
    for token in tokens:
        flag = token.split("=", 1)[0]
        typed_input = _FLAG_TO_TYPED_INPUT.get(flag)
        if typed_input is not None:
            shadowed.add((flag, typed_input))

    for flag, typed_input in sorted(shadowed):
        if typed_input in typed_inputs_present:
            errors.append(
                f"{label}: extra-args contains {flag!r}, which shadows the "
                f"typed {typed_input!r} input already set on this same "
                "step. Set only the typed input -- see this step's own "
                "history for why the two are not equivalent (a typed "
                "input can drive extra Action-side wiring, e.g. "
                "`public-header-dir` on `mode: scan`, that raw extra-args "
                "never gets)."
            )
        else:
            errors.append(
                f"{label}: extra-args contains {flag!r}, a contract-"
                f"defining flag with its own typed {typed_input!r} input. "
                "Use the typed input instead of extra-args, even though "
                "it isn't set elsewhere on this step -- extra-args is for "
                "temporary experimentation, not for values that define "
                "what is being compared (see README.md's extra-args note)."
            )
    return errors


def check(workflows: dict[Path, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for path, workflow in workflows.items():
        for label, with_block in _abicheck_steps(workflow, path):
            errors.extend(check_step(label, with_block))
    return errors


def main() -> int:
    workflows: dict[Path, dict[str, Any]] = {}
    for path in CHECKED_WORKFLOWS:
        if not path.exists():
            continue
        with path.open() as f:
            workflows[path] = yaml.safe_load(f)

    errors = check(workflows)
    if errors:
        print(
            f"check_recipe_parity: {len(errors)} problem(s) found:\n",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(
        "check_recipe_parity: OK -- no abicheck/abicheck step shadows a "
        "typed contract input through extra-args"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
