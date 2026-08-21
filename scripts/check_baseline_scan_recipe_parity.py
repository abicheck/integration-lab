#!/usr/bin/env python3
"""Check that baseline.yml's canonical `dump` step and abi-scan.yml's
canonical `scan` step -- the two halves of one logical comparison, `scan
--against` a `dump`-produced baseline -- actually resolve the same analysis
recipe on every field where they must agree.

Why this exists: the two workflows are hand-maintained, separate YAML
files. Their comments have said "these use the same recipe" for a long
time, and twice that turned out not to be true in ways CI didn't catch
until a PR downstream hit `NOT_COMPARABLE` (see abi-scan.yml's `scan` step
comment for the incident this repo is named for -- an `extra-args`-shadowed
typed input, and a redundant explicit header alongside `public-header-dir`,
both landed unnoticed because nothing compared the two workflows' actual
resolved inputs against each other). scripts/check_recipe_parity.py catches
the first shape (a contract flag shadowed through `extra-args`) generically
across every `abicheck/abicheck` step in every workflow; this script is
narrower and complementary -- it compares the two *specific* steps whose
outputs must be directly comparable, field by field, the way the assessment
that named this whole class of bug asked for ("Add a workflow-recipe parity
test... parse .../baseline.yml and .../abi-scan.yml and compare the
normalized contract inputs").

See scripts/CLAUDE.md's own convention: this is the *static* half of the
guard. scripts/build_bazel_evidence_pack.py's own callers producing a
genuinely different evidence pack per invocation is a separate, *dynamic*
question this script cannot answer from YAML alone -- that is what
baseline.yml's own "Round-trip certification" step (dump the artifact, scan
it against its own fresh dump, require NO_CHANGE) exists to certify instead.
Static + dynamic together are what actually closes this bug class: this
script catches an *authored* recipe drift before it ever runs; the
round-trip step catches anything this script's necessarily-approximate
field comparison can't see (a real divergence inside abicheck itself,
independent of what either workflow asked for).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "check_baseline_scan_recipe_parity: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
BASELINE_PATH = WORKFLOWS_DIR / "baseline.yml"
ABI_SCAN_PATH = WORKFLOWS_DIR / "abi-scan.yml"

# Fields that must resolve to the *identical* literal value on both sides --
# this is the "must agree" list the assessment named: scanner Action pin,
# artifact target, source root, public header roots, include roots,
# language/frontend, requested depth, policy, build target. `new-library`
# covers "artifact target"; `header`/`old-header`/`new-header` are
# deliberately NOT in this list -- dump's bare `header:` and scan's
# candidate-sided `new-header:` are legitimately different spellings of the
# same thing (the assessment's own "expected differences" list names this
# exact case: "candidate-sided header notation") -- so this script compares
# only the fields with no such legitimate spelling difference.
_MUST_MATCH_FIELDS = (
    "new-library",
    "public-header-dir",
    "sources",
    "include",
    "old-include",
    "new-include",
    "depth",
    "policy",
    "ast-frontend",
    "build-target",
    "lang",
)

# Fields the assessment itself names as *expected* to differ between a
# dump baseline step and a scan candidate step -- not compared at all, so a
# mismatch here is never even considered.
_EXPECTED_TO_DIFFER_FIELDS = frozenset(
    {
        "mode",
        "against",
        "since",
        "changed-path",
        "version",
        "old-version",
        "new-version",
        "output-file",
        "budget",
        "format",
        "fail-on-breaking",
        "fail-on-api-break",
        "add-job-summary",
        "pr-comment",
    }
)

_USES_RE = re.compile(r"^abicheck/abicheck@(?P<sha>[0-9a-f]{7,40})")


def _load(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f)


def _find_step(
    workflow: dict[str, Any], job_id: str, *, step_id: str | None = None, name_contains: str | None = None
) -> dict[str, Any] | None:
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    job = jobs.get(job_id) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return None
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step_id is not None and step.get("id") != step_id:
            continue
        if name_contains is not None and name_contains not in (step.get("name") or ""):
            continue
        if step_id is None and name_contains is None:
            continue
        return step
    return None


def _bazel_pack_root_target(workflow: dict[str, Any], job_id: str) -> str | None:
    """The `--root-target` value the job's own `bazel_pack` step's shell
    script passes to `build_bazel_evidence_pack.py` -- read out of the
    step's `run:` block, since that's the only place this value lives (it
    never reaches a typed Action input)."""
    step = _find_step(workflow, job_id, step_id="bazel_pack")
    if step is None:
        return None
    run = step.get("run")
    if not isinstance(run, str):
        return None
    m = re.search(r"--root-target\s+\"([^\"]+)\"", run)
    return m.group(1) if m else None


def check(baseline_wf: dict[str, Any], abi_scan_wf: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    dump_step = _find_step(baseline_wf, "collect", name_contains="Collect source-aware ABI baseline")
    if dump_step is None:
        return [f"{BASELINE_PATH}: could not find the canonical 'Collect source-aware ABI baseline' dump step in job 'collect'"]

    scan_step = _find_step(abi_scan_wf, "scan", step_id="scan")
    if scan_step is None:
        return [f"{ABI_SCAN_PATH}: could not find the canonical scan step (id: scan) in job 'scan'"]

    dump_with = dump_step.get("with") if isinstance(dump_step.get("with"), dict) else {}
    scan_with = scan_step.get("with") if isinstance(scan_step.get("with"), dict) else {}

    # Scanner Action pin: both steps must use the identical abicheck
    # commit. A silent pin drift between the baseline collector and the
    # canonical PR gate is exactly the kind of divergence this whole
    # script exists to catch -- two different abicheck versions can
    # disagree on comparability/fingerprinting logic for reasons neither
    # workflow's own diff shows.
    dump_uses = dump_step.get("uses")
    scan_uses = scan_step.get("uses")
    dump_sha = _USES_RE.match(dump_uses).group("sha") if isinstance(dump_uses, str) and _USES_RE.match(dump_uses) else None
    scan_sha = _USES_RE.match(scan_uses).group("sha") if isinstance(scan_uses, str) and _USES_RE.match(scan_uses) else None
    if dump_sha is None:
        errors.append(f"{BASELINE_PATH}: dump step's uses={dump_uses!r} doesn't match the expected abicheck/abicheck@<sha> shape")
    if scan_sha is None:
        errors.append(f"{ABI_SCAN_PATH}: scan step's uses={scan_uses!r} doesn't match the expected abicheck/abicheck@<sha> shape")
    if dump_sha is not None and scan_sha is not None and dump_sha != scan_sha:
        errors.append(
            f"SCANNER_PIN_MISMATCH: baseline.yml's dump step pins abicheck@{dump_sha} but "
            f"abi-scan.yml's scan step pins abicheck@{scan_sha} -- the baseline collector and "
            "the canonical PR gate must run the identical abicheck version"
        )

    for field in _MUST_MATCH_FIELDS:
        dump_val = dump_with.get(field)
        scan_val = scan_with.get(field)
        if dump_val == scan_val:
            continue
        errors.append(
            f"RECIPE_FIELD_MISMATCH: '{field}' -- baseline.yml's dump step has "
            f"{dump_val!r}, abi-scan.yml's scan step has {scan_val!r}. If this is a "
            "genuinely intentional difference, add it to _EXPECTED_TO_DIFFER_FIELDS "
            "with a comment explaining why; otherwise fix the drift."
        )

    # Header roots: dump-side spells the single-sided input `header:`,
    # scan-side spells its candidate-sided equivalent `new-header:`
    # (action.yml's own documented convention -- see abi-scan.yml's scan
    # step comment) -- a real, legitimate spelling difference, but NOT a
    # license to ignore the *value*. An earlier revision of this script
    # classified all three header inputs as unconditionally expected to
    # differ, which would have silently accepted a future PR re-adding a
    # redundant `new-header:` to abi-scan.yml alone -- recreating the
    # exact `include_sequence`/`NOT_COMPARABLE` bug this whole repo is
    # named for, undetected, since only abi-scan.yml would have changed
    # (Codex review, fresh evidence: "adding a redundant new-header only
    # to abi-scan.yml recreates the documented failure... the baseline
    # workflow's self-scan cannot detect the cross-workflow drift" --
    # true of the round-trip certification step too, since that step only
    # scans against abi-scan.yml's own baseline dump on the *same* side of
    # the drift, not against a second, independently-derived recipe).
    # Normalize dump's `header` against scan's `new-header` and compare
    # the values directly -- covers both currently unset (today's fixed
    # state) and both set to the identical explicit path; a one-sided or
    # differing value on either is a real mismatch.
    dump_header = dump_with.get("header")
    scan_header = scan_with.get("new-header")
    if dump_header != scan_header:
        errors.append(
            "RECIPE_FIELD_MISMATCH: header roots -- baseline.yml's dump step has "
            f"header={dump_header!r}, abi-scan.yml's scan step has "
            f"new-header={scan_header!r}. `new-header` is scan's documented "
            "candidate-sided spelling of dump's `header` -- the values themselves "
            "must still agree (including both being unset)."
        )
    # `old-header` never applies to either canonical step (dump has no old
    # side; scan's canonical step compares against a persisted JSON
    # baseline, never an `old-header`-parsed live binary) -- flagged as a
    # real, unexpected usage-drift if it ever appears on either.
    for step_path, step_with in ((BASELINE_PATH, dump_with), (ABI_SCAN_PATH, scan_with)):
        if step_with.get("old-header") is not None:
            errors.append(
                f"{step_path}: canonical step unexpectedly sets old-header="
                f"{step_with['old-header']!r} -- neither the dump baseline step nor "
                "the scan candidate step has an old-side live binary to parse"
            )

    # Every key actually present on either side must be accounted for by
    # this script (either checked for equality, or explicitly named as
    # expected to differ) -- an unrecognized key silently skips both,
    # which would make this check quietly incomplete as new inputs are
    # added to either step over time. `build-info`/`header`/`new-header`/
    # `old-header` are handled specially above (their literal values
    # can't be compared through the generic equality loop, or need
    # cross-field normalization) -- still "accounted for", so they're
    # excluded here rather than falling through as unclassified.
    known = set(_MUST_MATCH_FIELDS) | _EXPECTED_TO_DIFFER_FIELDS | {"build-info", "header", "new-header", "old-header"}
    unaccounted = (set(dump_with) | set(scan_with)) - known
    for field in sorted(unaccounted):
        errors.append(
            f"UNCLASSIFIED_FIELD: '{field}' is set on the dump and/or scan step but "
            "this script doesn't know whether it must match or is expected to differ "
            "-- add it to _MUST_MATCH_FIELDS or _EXPECTED_TO_DIFFER_FIELDS"
        )

    # Build evidence source: both must derive from the same evidence-pack
    # step output, scoped to the same Bazel root target -- the literal
    # `${{ steps.X.outputs.Y }}` expression can't be compared textually
    # across two workflows with independent step namespaces, but the step
    # id convention (`bazel_pack`) and the --root-target argument its own
    # shell script passes to build_bazel_evidence_pack.py can be.
    dump_build_info = dump_with.get("build-info")
    scan_build_info = scan_with.get("build-info")
    _EXPECTED_BUILD_INFO_EXPR = "${{ steps.bazel_pack.outputs.pack_dir }}"
    if dump_build_info != _EXPECTED_BUILD_INFO_EXPR:
        errors.append(
            f"{BASELINE_PATH}: dump step's build-info={dump_build_info!r}, expected "
            f"{_EXPECTED_BUILD_INFO_EXPR!r} (steps.bazel_pack.outputs.pack_dir)"
        )
    if scan_build_info != _EXPECTED_BUILD_INFO_EXPR:
        errors.append(
            f"{ABI_SCAN_PATH}: scan step's build-info={scan_build_info!r}, expected "
            f"{_EXPECTED_BUILD_INFO_EXPR!r} (steps.bazel_pack.outputs.pack_dir)"
        )
    dump_root_target = _bazel_pack_root_target(baseline_wf, "collect")
    scan_root_target = _bazel_pack_root_target(abi_scan_wf, "scan")
    if dump_root_target is None:
        errors.append(f"{BASELINE_PATH}: could not find --root-target in job 'collect''s bazel_pack step")
    if scan_root_target is None:
        errors.append(f"{ABI_SCAN_PATH}: could not find --root-target in job 'scan''s bazel_pack step")
    if dump_root_target is not None and scan_root_target is not None and dump_root_target != scan_root_target:
        errors.append(
            "BUILD_EVIDENCE_ROOT_TARGET_MISMATCH: baseline.yml's evidence pack is scoped "
            f"to --root-target {dump_root_target!r} but abi-scan.yml's is scoped to "
            f"{scan_root_target!r} -- the baseline and the canonical scan must collect L3 "
            "evidence for the same Bazel target"
        )

    return errors


def main() -> int:
    baseline_wf = _load(BASELINE_PATH)
    abi_scan_wf = _load(ABI_SCAN_PATH)

    errors = check(baseline_wf, abi_scan_wf)
    if errors:
        print(
            f"check_baseline_scan_recipe_parity: {len(errors)} problem(s) found:\n",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(
        "check_baseline_scan_recipe_parity: OK -- baseline.yml's dump step and "
        "abi-scan.yml's scan step agree on every contract-defining field"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
