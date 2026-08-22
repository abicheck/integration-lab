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
# mismatch here is never even considered. `mode`/`against`/
# `fail-on-breaking`/`fail-on-api-break` are handled specially (below, not
# through this set): each must always differ, but only as one specific
# expected shape -- see `known`'s own comment.
_EXPECTED_TO_DIFFER_FIELDS = frozenset(
    {
        "since",
        "changed-path",
        "version",
        "old-version",
        "new-version",
        "output-file",
        "budget",
        "format",
        "add-job-summary",
        "pr-comment",
    }
)

# `abi-scan.yml`'s own `Enforce gate` step only fails the required PR check
# when `steps.scan.outcome == 'failure'` -- and the scanner Action itself
# only exits non-zero on a BREAKING/API_BREAK verdict when these two flags
# are `true` (Codex review, fresh evidence, P1). Blanket-classifying them
# as "expected to differ" let this static gate accept a canonical scan
# silently changed to `fail-on-breaking: false`/`fail-on-api-break: false`
# -- a real ABI break would then leave the scan step successful and the
# required PR gate green, with nothing in this checker (or the round-trip
# certification, which exercises `baseline.yml`, not `abi-scan.yml`)
# catching it. The dump step's own `fail-on-breaking: true` is left
# unconstrained -- dump mode has no comparison to gate on, so its value
# there is inert either way; only the scan side's gating actually matters.
_REQUIRED_SCAN_GATE_FLAGS = ("fail-on-breaking", "fail-on-api-break")

# The one trusted baseline artifact the canonical scan step may compare
# against -- produced by `.github/actions/resolve-baseline` from the PR's
# base SHA, not attacker-controlled. `against` was previously blanket-
# exempted as "expected to differ", which let this static gate accept a
# canonical scan silently repointed at any other path -- e.g. the
# repo-committed `abi/math.abicheck.json` (a working-tree file the PR
# itself can edit), which would let the same PR rewrite its own baseline
# to conceal a real ABI break, contradicting the trusted-baseline
# invariant `docs/canonical-bazel-gate.md` documents (Codex review, fresh
# evidence, P1). Handled the same way `mode` already is: the dump step
# must have no `against` at all (dump mode never compares against
# anything), and the scan step's `against` must be exactly this one
# resolve-baseline-produced path.
_EXPECTED_SCAN_AGAINST = "${{ runner.temp }}/math.base.abicheck.json"

# Anchored at both ends (Codex review, fresh evidence): an unanchored
# match would extract a matching prefix from two genuinely different refs
# that merely *start* with the same seven hex characters (e.g.
# `@abcdef0-baseline` vs. `@abcdef0-scan`) and report them as the same
# pin. The full ref must be nothing but a 7-40 character hex SHA.
_USES_RE = re.compile(r"^abicheck/abicheck@(?P<sha>[0-9a-f]{7,40})$")


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


def _bazel_pack_root_targets(workflow: dict[str, Any], job_id: str) -> list[str] | None:
    """Every `--root-target` value the job's own `bazel_pack` step's shell
    script passes to `build_bazel_evidence_pack.py`, in argv order -- read
    out of the step's `run:` block, since that's the only place this value
    lives (it never reaches a typed Action input). `--root-target` is
    documented and implemented (`action="append"`) as repeatable (Codex
    review, fresh evidence): comparing only the first occurrence would
    silently ignore a drift in any *later* one, since both workflows'
    evidence pack is scoped to the FULL set of roots passed, not just the
    first. Returns `None` (not `[]`) when the step is missing entirely, so
    a caller can still distinguish "no bazel_pack step" from "a bazel_pack
    step with zero --root-target flags"."""
    step = _find_step(workflow, job_id, step_id="bazel_pack")
    if step is None:
        return None
    run = step.get("run")
    if not isinstance(run, str):
        return None
    return re.findall(r"--root-target\s+\"([^\"]+)\"", run)


# Matches a `bazel cquery`/`bazel aquery` invocation and captures both the
# command itself and its full redirect target -- both real commands are
# multi-line, backslash-continued shell, so this spans lines. Normalized by
# collapsing all whitespace before comparison, since line-wrapping/
# indentation differences between the two workflows' `run:` blocks are not
# themselves a recipe drift.
_BAZEL_QUERY_RE = re.compile(r"(bazel (?:cquery|aquery)\b.*?)\s*>\s*\"?(\$RUNNER_TEMP/\S+?)\"?\s*(?:\n|$)", re.DOTALL)
# The pip install line -- captures the whole `pip install ...` invocation,
# which is where the abicheck git ref used to *build the evidence pack
# itself* (a separate pin from the `uses: abicheck/abicheck@<sha>` scanner
# Action pin already checked above) lives. Matches any `pip install` line
# whose eventual VCS requirement resolves the `abicheck` repo -- both the
# PEP 508 form (`abicheck @ git+https://.../abicheck.git@<ref>`) and a
# bare VCS-URL reinstall (`pip install --force-reinstall
# git+https://.../abicheck.git@<ref>`, which pip's own `<vcs project url>`
# operand accepts identically -- Codex review, fresh evidence: the PEP-508-
# only pattern silently ignored this second, equally-real spelling, so a
# later bare-URL reinstall replacing the package actually used to build
# the evidence pack went unchecked). Doesn't require the literal
# `abicheck @ ` prefix -- only that the URL eventually names the
# `abicheck` repo, which both spellings share.
#
# The ref itself is OPTIONAL and unconstrained to a hex SHA (Codex review,
# fresh evidence, a second round on this same pattern): pip accepts a
# named ref (`@main`) or no `@ref` suffix at all (installing from the
# repo's own default branch) exactly as validly as a pinned SHA -- an
# earlier revision of this pattern required `@[0-9a-f]{7,40}`, which
# silently ignored either of those, so a later reinstall onto a moving
# ref went unrecognized and uncompared. `(?!/)` immediately after
# `/abicheck` is load-bearing, not cosmetic: with the trailing `.git`/`@ref`
# both optional, a naive `/abicheck` alone is satisfied by the *organization*
# segment of `.../abicheck/abicheck...` (non-greedy `\S*?` takes the
# shortest match), so without it this pattern would silently truncate at
# `.../github.com/abicheck` and never see the real repo name or its ref at
# all -- confirmed by testing without the lookahead before adding it.
# DOTALL (Codex review, fresh evidence): a real reinstall is often spelled
# with an ordinary shell line continuation (`pip install --force-reinstall
# \` then the VCS URL on the next line) -- without DOTALL, `.` cannot cross
# that newline, so the whole continued command was invisible to this
# pattern and a reinstall onto a different ref went unrecognized entirely.
# Residual, accepted imprecision: since `.*?` can now cross an intervening,
# unrelated line too, a `pip install` with no ref of its own (`pip install
# wheel`) can have its OWN match span forward and swallow an unrelated
# line before landing on the *next* real install's ref -- cosmetically
# odd (that match's displayed text mixes two commands), but harmless for
# correctness: `finditer`'s non-overlapping matches still produce one
# match per real ref present, in order, so `matches[-1]` (the actual
# comparison target) is unaffected either way. Bounded to one step's
# `run:` text (never spans across steps), so the blast radius of the
# imprecision is small.
_PIP_INSTALL_RE = re.compile(r"pip install\b.*?git\+\S*?/abicheck(?!/)(?:\.git)?(?:@\S+)?", re.IGNORECASE | re.DOTALL)


def _normalize_shell(text: str) -> str:
    return " ".join(text.split())


def _bazel_query_commands(workflow: dict[str, Any], job_id: str, *, expected_paths: dict[str, str]) -> dict[str, str]:
    """The normalized `bazel cquery`/`bazel aquery` command lines from the
    job's own `bazel_queries` step, keyed by query kind ('cquery'/
    'aquery') -- selecting, for each kind, the invocation whose redirect
    matches `expected_paths[kind]` (the exact path the job's own
    `bazel_pack` step consumes as its `--cquery`/`--aquery` input), not
    merely the last invocation of that kind (Codex review, fresh evidence:
    a step containing an extra diagnostic query of the same kind writing a
    *different* file -- e.g. abi-scan.yml's own trailing `buildfiles(...)`
    query is a `bazel query`, not `cquery`/`aquery`, but a hypothetical
    same-kind diagnostic would reproduce this -- must not silently become
    "the" compared command just because it appears later in the script).
    Empty dict if the step is missing; a kind absent from the result means
    no invocation of that kind redirected to the expected path."""
    step = _find_step(workflow, job_id, step_id="bazel_queries")
    run = step.get("run") if isinstance(step, dict) else None
    if not isinstance(run, str):
        return {}
    commands: dict[str, str] = {}
    for m in _BAZEL_QUERY_RE.finditer(run):
        normalized = _normalize_shell(m.group(1))
        redirect = m.group(2)
        kind = "cquery" if normalized.startswith("bazel cquery") else "aquery"
        if expected_paths.get(kind) is not None and redirect == expected_paths[kind]:
            commands[kind] = normalized
    return commands


def _abicheck_pip_pin(workflow: dict[str, Any], job_id: str) -> str | None:
    """The normalized `pip install ...` line from the job's own
    `abicheck_pip` step that actually determines what's installed -- the
    abicheck git ref used to *run build_bazel_evidence_pack.py*,
    independent of the scanner Action's own `uses: abicheck/abicheck@<sha>`
    pin checked above.

    Uses `finditer` and takes the LAST match, not the first (Codex review,
    fresh evidence): a step containing two abicheck installs on separate
    lines has its *later* one win (pip install reinstalls/overwrites), so
    a `.search()`-based first match would keep comparing a stale earlier
    pin while the ref that actually runs the evidence-pack helper script
    -- the later one -- went unchecked."""
    step = _find_step(workflow, job_id, step_id="abicheck_pip")
    run = step.get("run") if isinstance(step, dict) else None
    if not isinstance(run, str):
        return None
    matches = list(_PIP_INSTALL_RE.finditer(run))
    return _normalize_shell(matches[-1].group(0)) if matches else None


def _bazel_pack_query_inputs(workflow: dict[str, Any], job_id: str) -> dict[str, str]:
    """The `--cquery`/`--aquery` path arguments the job's own `bazel_pack`
    step passes to `build_bazel_evidence_pack.py` -- the exact evidence
    files it actually consumes, used to pick out which `bazel_queries`
    invocation of each kind is "the" one being compared (see
    `_bazel_query_commands`'s own docstring)."""
    step = _find_step(workflow, job_id, step_id="bazel_pack")
    run = step.get("run") if isinstance(step, dict) else None
    if not isinstance(run, str):
        return {}
    paths: dict[str, str] = {}
    for kind in ("cquery", "aquery"):
        m = re.search(rf"--{kind}\s+\"([^\"]+)\"", run)
        if m:
            paths[kind] = m.group(1)
    return paths


_BAZEL_BUILD_MATH_RE = re.compile(r"^\s*bazel build //:math\b")


def _bazel_build_math_command(workflow: dict[str, Any], job_id: str) -> str | None:
    """The job's own `bazel build //:math ...` step that produces
    `bazel-bin/libmath.so` -- the literal build recipe behind
    `new-library`. Neither canonical job gives this step an `id`, so it's
    found by scanning the job's steps directly for the first `run:` whose
    text starts with `bazel build //:math` (Codex review, fresh evidence:
    the parity check compared how the evidence pack was collected and how
    the two Action steps were invoked, but never the Bazel command that
    actually produces the artifact both of those steps consume -- a
    candidate build silently gaining an ABI-affecting flag, e.g.
    `--config=asan` or an extra `--cxxopt`, would still produce
    `bazel-bin/libmath.so` and pass every other check here, even though
    the scan and baseline artifacts were no longer built the same way).
    Returns `None` if the job has no such step."""
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    job = jobs.get(job_id) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return None
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str) and _BAZEL_BUILD_MATH_RE.match(run):
            return _normalize_shell(run)
    return None


def _bazel_pack_script(workflow: dict[str, Any], job_id: str) -> str | None:
    """The job's own `bazel_pack` step script, normalized, with the
    `--root-target` VALUE masked out -- that value is already compared
    directly by `_bazel_pack_root_targets`/`BUILD_EVIDENCE_ROOT_TARGET_
    MISMATCH` above, so masking it here avoids reporting the identical
    drift twice under two different error codes."""
    step = _find_step(workflow, job_id, step_id="bazel_pack")
    run = step.get("run") if isinstance(step, dict) else None
    if not isinstance(run, str):
        return None
    masked = re.sub(r'--root-target\s+"[^"]*"', "--root-target <masked>", run)
    return _normalize_shell(masked)


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

    # mode: must be exactly the (dump, scan) pair, not merely "differs"
    # (Codex review, fresh evidence: a bare inequality check would accept
    # baseline.yml's collector being accidentally changed to `mode: scan`
    # -- a real misconfiguration `check_recipe_parity.py`'s own
    # `_abicheck_steps` step-shape scan wouldn't catch either, since the
    # step would still be a syntactically valid `abicheck/abicheck` call.
    # `baseline.yml` doesn't run on pull requests, so this static check is
    # the only thing that would catch it before it broke the real baseline
    # job on the next push to `main`).
    dump_mode = dump_with.get("mode")
    scan_mode = scan_with.get("mode")
    if (dump_mode, scan_mode) != ("dump", "scan"):
        errors.append(
            f"MODE_MISMATCH: expected baseline.yml's dump step to have mode='dump' "
            f"and abi-scan.yml's scan step to have mode='scan', got "
            f"({dump_mode!r}, {scan_mode!r})"
        )

    # against: must be the one trusted resolve-baseline-produced path, not
    # merely "differs" -- see `_EXPECTED_SCAN_AGAINST`'s own comment (Codex
    # review, fresh evidence, P1). Blanket-exempting this field let a
    # canonical scan step be silently repointed at any other baseline
    # (including a PR-controlled working-tree file), defeating the
    # trusted-baseline invariant this whole gate exists to protect.
    dump_against = dump_with.get("against")
    scan_against = scan_with.get("against")
    if dump_against is not None or scan_against != _EXPECTED_SCAN_AGAINST:
        errors.append(
            "AGAINST_MISMATCH: expected baseline.yml's dump step to have no "
            f"'against' (dump mode never compares against anything) and "
            f"abi-scan.yml's scan step to have against={_EXPECTED_SCAN_AGAINST!r} "
            f"(the trusted resolve-baseline artifact), got "
            f"(against={dump_against!r}, against={scan_against!r})"
        )

    # fail-on-breaking/fail-on-api-break: the canonical scan step must gate
    # on both -- see `_REQUIRED_SCAN_GATE_FLAGS`'s own comment. The dump
    # step's own value is deliberately unconstrained here (dump mode has
    # nothing to gate on).
    for flag in _REQUIRED_SCAN_GATE_FLAGS:
        if scan_with.get(flag) is not True:
            errors.append(
                f"SCAN_GATE_FLAG_MISMATCH: abi-scan.yml's scan step must have "
                f"{flag}: true (got {scan_with.get(flag)!r}) -- without it, a real "
                "BREAKING/API_BREAK verdict would leave the scan step successful "
                "and the required PR gate green"
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
    # Role-invalid spellings: a plain `header:` on the *scan* step (rather
    # than `new-header:`) still reaches the Action as a real `-H` root --
    # action.yml doesn't reject it -- so it would change scan's effective
    # header roots exactly like a `new-header:` would, but silently, since
    # the check above only ever reads scan_with["new-header"] (Codex
    # review, fresh evidence: "both values compared here remain None, and
    # the parity check therefore passes even though the Action forwards
    # the generic typed header... changing the scan's effective header
    # roots without a matching dump-side change"). The converse
    # (`new-header:` on the *dump* step, which has no old/new side at all)
    # is equally role-invalid. Both are rejected outright, independent of
    # value, rather than folded into the normalized comparison above --
    # each is a usage error on its own step, not a cross-step drift.
    if "header" in scan_with:
        errors.append(
            f"{ABI_SCAN_PATH}: scan step sets header={scan_with['header']!r} -- "
            "scan's own candidate-sided input is `new-header:`, not the generic "
            "`header:` (which the Action still forwards as a real -H root)"
        )
    if "new-header" in dump_with:
        errors.append(
            f"{BASELINE_PATH}: dump step sets new-header={dump_with['new-header']!r} "
            "-- dump has no old/new side; its own input is the generic `header:`"
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
    # added to either step over time. `mode`/`against`/`fail-on-breaking`/
    # `fail-on-api-break`/`build-info`/`header`/`new-header`/`old-header`
    # are all handled specially above (`mode`/`against` each need an
    # exact-shape assertion, not mere inequality; the gate flags need a
    # required-true assertion on the scan side only; the header fields
    # need cross-field/role normalization; `build-info`'s literal value
    # can't be compared through the generic equality loop) -- still
    # "accounted for", so they're excluded here rather than falling
    # through as unclassified.
    known = (
        set(_MUST_MATCH_FIELDS)
        | _EXPECTED_TO_DIFFER_FIELDS
        | set(_REQUIRED_SCAN_GATE_FLAGS)
        | {"mode", "against", "build-info", "header", "new-header", "old-header"}
    )
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
    dump_root_targets = _bazel_pack_root_targets(baseline_wf, "collect")
    scan_root_targets = _bazel_pack_root_targets(abi_scan_wf, "scan")
    if not dump_root_targets:
        errors.append(f"{BASELINE_PATH}: could not find --root-target in job 'collect''s bazel_pack step")
    if not scan_root_targets:
        errors.append(f"{ABI_SCAN_PATH}: could not find --root-target in job 'scan''s bazel_pack step")
    if dump_root_targets and scan_root_targets and dump_root_targets != scan_root_targets:
        errors.append(
            "BUILD_EVIDENCE_ROOT_TARGET_MISMATCH: baseline.yml's evidence pack is scoped "
            f"to --root-target {dump_root_targets!r} but abi-scan.yml's is scoped to "
            f"{scan_root_targets!r} -- the baseline and the canonical scan must collect L3 "
            "evidence for the identical, identically-ordered set of Bazel targets "
            "(--root-target is repeatable)"
        )

    # Complete evidence-pack *producer* recipe (Codex review, fresh
    # evidence): everything above compares the two `abicheck/abicheck`
    # Action steps' own typed inputs -- but both steps consume an evidence
    # pack a job first assembles itself, from a `bazel_queries` step (the
    # actual cquery/aquery command lines), an `abicheck_pip` step (a
    # *second*, independent abicheck git-ref pin -- the one used to *run*
    # build_bazel_evidence_pack.py, not the one that scans/dumps), and a
    # `bazel_pack` step (the helper script invocation itself). A drift in
    # any of these changes what evidence the dump/scan steps see without
    # touching either step's own `with:` block at all, invisible to every
    # check above. abi-scan.yml's `bazel_queries` step legitimately has one
    # extra trailing `bazel query 'buildfiles(...)'` line (feeds
    # check_coverage_contract.py's --buildfiles, which baseline.yml's dump
    # mode has no use for -- dump never runs crosschecks) -- that's why
    # the two steps' cquery/aquery commands are compared individually
    # rather than as whole-script text.
    dump_query_inputs = _bazel_pack_query_inputs(baseline_wf, "collect")
    scan_query_inputs = _bazel_pack_query_inputs(abi_scan_wf, "scan")
    dump_queries = _bazel_query_commands(baseline_wf, "collect", expected_paths=dump_query_inputs)
    scan_queries = _bazel_query_commands(abi_scan_wf, "scan", expected_paths=scan_query_inputs)
    for kind in ("cquery", "aquery"):
        dump_q = dump_queries.get(kind)
        scan_q = scan_queries.get(kind)
        if dump_q is None:
            errors.append(
                f"{BASELINE_PATH}: could not find a 'bazel {kind}' command in job 'collect''s "
                f"bazel_queries step that redirects to {dump_query_inputs.get(kind)!r} (the path "
                f"its own bazel_pack step reads as --{kind})"
            )
        if scan_q is None:
            errors.append(
                f"{ABI_SCAN_PATH}: could not find a 'bazel {kind}' command in job 'scan''s "
                f"bazel_queries step that redirects to {scan_query_inputs.get(kind)!r} (the path "
                f"its own bazel_pack step reads as --{kind})"
            )
        if dump_q is not None and scan_q is not None and dump_q != scan_q:
            errors.append(
                f"BUILD_EVIDENCE_QUERY_MISMATCH: baseline.yml's and abi-scan.yml's 'bazel {kind}' "
                f"commands differ -- baseline.yml: {dump_q!r}, abi-scan.yml: {scan_q!r}. The two "
                "workflows must collect L3 evidence with the identical query, or the resulting "
                "evidence packs can silently cover different compile units."
            )

    dump_pip_pin = _abicheck_pip_pin(baseline_wf, "collect")
    scan_pip_pin = _abicheck_pip_pin(abi_scan_wf, "scan")
    if dump_pip_pin is None:
        errors.append(f"{BASELINE_PATH}: could not find a 'pip install ... abicheck @ git+...' line in job 'collect''s abicheck_pip step")
    if scan_pip_pin is None:
        errors.append(f"{ABI_SCAN_PATH}: could not find a 'pip install ... abicheck @ git+...' line in job 'scan''s abicheck_pip step")
    if dump_pip_pin is not None and scan_pip_pin is not None and dump_pip_pin != scan_pip_pin:
        errors.append(
            "BUILD_EVIDENCE_PIP_PIN_MISMATCH: baseline.yml's and abi-scan.yml's abicheck_pip "
            f"steps install different abicheck refs -- baseline.yml: {dump_pip_pin!r}, "
            f"abi-scan.yml: {scan_pip_pin!r}. This is the abicheck version that actually *runs* "
            "build_bazel_evidence_pack.py (separate from the abicheck/abicheck@<sha> scanner "
            "Action pin already checked above) -- a drift here can change the evidence pack's "
            "own shape without either canonical step's `uses:` pin ever changing."
        )

    # The artifact-producing Bazel build itself (Codex review, fresh
    # evidence): everything above compares how the evidence pack was
    # collected and how the two Action steps were invoked, but never the
    # `bazel build //:math` command that actually produces
    # `bazel-bin/libmath.so` -- the literal artifact `new-library` points
    # at. A candidate build silently gaining an ABI-affecting flag (e.g.
    # `--config=asan`, an extra `--cxxopt`) still produces the identical
    # output path and would pass every check above unnoticed.
    dump_build_cmd = _bazel_build_math_command(baseline_wf, "collect")
    scan_build_cmd = _bazel_build_math_command(abi_scan_wf, "scan")
    if dump_build_cmd is None:
        errors.append(f"{BASELINE_PATH}: could not find a 'bazel build //:math' step in job 'collect'")
    if scan_build_cmd is None:
        errors.append(f"{ABI_SCAN_PATH}: could not find a 'bazel build //:math' step in job 'scan'")
    if dump_build_cmd is not None and scan_build_cmd is not None and dump_build_cmd != scan_build_cmd:
        errors.append(
            "BUILD_EVIDENCE_ARTIFACT_BUILD_MISMATCH: baseline.yml's and abi-scan.yml's "
            f"'bazel build //:math' commands differ -- baseline.yml: {dump_build_cmd!r}, "
            f"abi-scan.yml: {scan_build_cmd!r}. The baseline and the canonical scan must build "
            "bazel-bin/libmath.so with the identical Bazel invocation, or the two artifacts "
            "are not the same recipe even when every other field agrees."
        )

    dump_pack_script = _bazel_pack_script(baseline_wf, "collect")
    scan_pack_script = _bazel_pack_script(abi_scan_wf, "scan")
    if dump_pack_script is None:
        errors.append(f"{BASELINE_PATH}: could not find a script in job 'collect''s bazel_pack step")
    if scan_pack_script is None:
        errors.append(f"{ABI_SCAN_PATH}: could not find a script in job 'scan''s bazel_pack step")
    if dump_pack_script is not None and scan_pack_script is not None and dump_pack_script != scan_pack_script:
        errors.append(
            "BUILD_EVIDENCE_PACK_SCRIPT_MISMATCH: baseline.yml's and abi-scan.yml's bazel_pack "
            f"steps invoke build_bazel_evidence_pack.py differently (--root-target masked out, "
            "already checked separately) -- baseline.yml: "
            f"{dump_pack_script!r}, abi-scan.yml: {scan_pack_script!r}"
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
