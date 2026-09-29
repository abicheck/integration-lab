#!/usr/bin/env python3
"""Recipe-parity guards between the committed `math` baseline
(baseline.yml, `mode: dump`) and the canonical PR gate (abi-scan.yml,
`mode: compare` -- `mode: scan` until upstream ADR-068 removed it).

Two checks, both static and mechanical:

1. **No contract-defining flag through `extra-args`.** Historical root cause
   of the recurring NOT_COMPARABLE / `include_sequence` mismatch: the scan
   step passed `--public-header-dir include` through raw `extra-args`
   instead of the typed input, so the Action-side wiring the typed input
   drives (an extra candidate-sided `-H new=...` root) never happened and
   the two sides were extracted through two different effective recipes
   even though both workflows *looked* the same. Prose ("this is the same
   recipe") is not a contract, only a check like this one is. For every
   `abicheck/abicheck` step, `extra-args` may not contain a flag that a
   typed input exists for.

2. **Header-root parity for the two-sided gate** (added with the
   2026-09-29 scan -> compare migration). A two-sided compare has no
   `public-header-dir` input (upstream rejects it outside dump/audit); the
   parity-preserving replacement is `new-header:` naming the SAME
   directory the baseline dump passes as `public-header-dir` -- and no
   extra header root beside it, which is what previously derived an extra
   `-isystem` include seed and NOT_COMPARABLE. So every two-sided
   `mode: compare` step with `depth: source` whose `old-library` is the
   trusted `math.base.abicheck.json` must set `new-header` to exactly the
   baseline's `public-header-dir`, must not set `header`/`public-header-dir`
   itself, and no step anywhere may still use the retired `mode: scan`
   (which upstream now fails outright).

This does not attempt to model the Action's full input surface or resolve
an effective analysis recipe (that belongs in abicheck core).
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


def _discover_workflows() -> tuple[Path, ...]:
    """Every `.yml`/`.yaml` file under `.github/workflows/` -- discovered,
    not hand-listed (Codex review, fresh evidence): an earlier revision of
    this check named the workflow files explicitly, on the reasoning that a
    new workflow adding an `abicheck/abicheck` step should be a *deliberate*
    addition to the list. That reasoning was backwards for a check whose
    whole job is catching an accidental mistake -- a fifth workflow could
    introduce the exact `extra-args` regression this guard exists for while
    the hand-maintained list silently never looked at it, and nothing would
    fail to say so. Discovering the workflow directory instead means a new
    workflow is checked automatically, with no list to remember to update."""
    if not WORKFLOWS_DIR.is_dir():
        return ()
    return tuple(sorted({*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")}))


CHECKED_WORKFLOWS = _discover_workflows()

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


# Matches a GitHub Actions expression (`${{ ... }}`), non-greedy so two
# separate expressions on one line don't collapse into one match.
_GHA_EXPRESSION_RE = re.compile(r"\$\{\{.*?\}\}")


def _extra_args_tokens(with_block: dict[str, Any]) -> list[str]:
    raw = with_block.get("extra-args")
    if not isinstance(raw, str) or not raw.strip():
        return []
    # A GitHub Actions expression's *value* (`${{ ... }}`) can't be
    # tokenized statically -- runtime-substituted, so this check has no way
    # to know what it expands to. But blanking the whole `extra-args`
    # string over one embedded expression (an earlier revision of this
    # check did exactly that) throws away every *other*, statically visible
    # token on the same line -- `extra-args: '--public-header-dir "${{
    # inputs.public_dir }}"'` would then miss the unambiguous, literal
    # `--public-header-dir` flag right next to the dynamic part (Codex
    # review, fresh evidence). Mask only the expression span itself with an
    # opaque placeholder token, so shlex still sees every literal token
    # around it, and the placeholder itself never collides with a real flag
    # spelling in `_FLAG_TO_TYPED_INPUT`.
    masked = _GHA_EXPRESSION_RE.sub("__gha_expr__", raw)
    try:
        return shlex.split(masked)
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
                "input can drive extra Action-side wiring (historically "
                "`public-header-dir` on the retired `mode: scan`) that raw "
                "extra-args never gets)."
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


#: The trusted, base-commit baseline file every gate-shaped compare reads
#: (resolve-baseline's `baseline-temp-filename` in abi-scan.yml).
_MATH_BASELINE_SUFFIX = "/math.base.abicheck.json"


def _baseline_public_header_dir(workflows: dict[Path, dict[str, Any]]) -> tuple[str | None, list[str]]:
    """`public-header-dir` of baseline.yml's source-depth `math` dump."""
    errors: list[str] = []
    found: set[str] = set()
    for path, workflow in workflows.items():
        if path.name != "baseline.yml":
            continue
        for label, with_block in _abicheck_steps(workflow, path):
            if (
                with_block.get("mode") == "dump"
                and with_block.get("new-library") == "bazel-bin/libmath.so"
                and with_block.get("depth") == "source"
            ):
                value = with_block.get("public-header-dir")
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{label}: source-depth math baseline dump sets no public-header-dir")
                else:
                    found.add(value.strip())
    if len(found) > 1:
        errors.append(f"baseline.yml: conflicting math public-header-dir values {sorted(found)}")
    return (next(iter(found)) if len(found) == 1 else None), errors


def check_header_root_parity(workflows: dict[Path, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for path, workflow in workflows.items():
        for label, with_block in _abicheck_steps(workflow, path):
            if with_block.get("mode") == "scan":
                errors.append(
                    f"{label}: `mode: scan` was removed upstream (ADR-068, no "
                    "deprecation window) and now fails the step; use "
                    "`mode: compare` with the baseline as `old-library`."
                )
    has_baseline_workflow = any(p.name == "baseline.yml" for p in workflows)
    if not has_baseline_workflow:
        return errors
    public_dir, baseline_errors = _baseline_public_header_dir(workflows)
    errors.extend(baseline_errors)
    gate_steps = 0
    for path, workflow in workflows.items():
        for label, with_block in _abicheck_steps(workflow, path):
            old = with_block.get("old-library")
            if not (
                with_block.get("mode", "compare") == "compare"
                and with_block.get("depth") == "source"
                and isinstance(old, str)
                and old.strip().endswith(_MATH_BASELINE_SUFFIX)
            ):
                continue
            gate_steps += 1
            for forbidden in ("header", "public-header-dir"):
                if with_block.get(forbidden) not in (None, ""):
                    errors.append(
                        f"{label}: sets {forbidden!r} on a two-sided source-depth "
                        "compare against the math baseline; use exactly "
                        "`new-header: <baseline public-header-dir>` instead."
                    )
            new_header = with_block.get("new-header")
            if public_dir is not None and (
                not isinstance(new_header, str) or new_header.strip() != public_dir
            ):
                errors.append(
                    f"{label}: new-header is {new_header!r}, but the baseline dump's "
                    f"public-header-dir is {public_dir!r} -- the candidate must see the "
                    "same single directory root (and nothing else) or the two sides "
                    "diverge on include_sequence."
                )
    if public_dir is not None and gate_steps == 0:
        errors.append(
            "no two-sided source-depth compare against math.base.abicheck.json "
            "was found -- the parity check has nothing to check (retarget it)."
        )
    return errors


def main() -> int:
    workflows: dict[Path, dict[str, Any]] = {}
    for path in CHECKED_WORKFLOWS:
        if not path.exists():
            continue
        with path.open() as f:
            workflows[path] = yaml.safe_load(f)

    errors = check(workflows) + check_header_root_parity(workflows)
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
        "typed contract input through extra-args, and the source-depth gate's "
        "header root matches the baseline's"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
