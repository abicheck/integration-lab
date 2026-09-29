# The canonical gate: `.github/workflows/abi-scan.yml`

This document is the detailed reference for the repository's one required,
load-bearing compatibility gate. If you only need the big picture, read the
root [README.md](../README.md) first; come here for the mechanics.

Everything described here is implemented and running today. Nothing in this
file is aspirational.

## What "canonical" means here

`abi-scan.yml`'s `scan` job (the job id and required check name are kept
for branch protection) runs exactly one gating `abicheck` invocation — a
two-sided `mode: compare` (`old-library:` = the trusted base-commit
baseline, `depth: source`, `since: <base SHA>`, `format: json`,
`abicheck-report.json`) against the root Bazel build (`//:math`). It was
`mode: scan` until upstream ADR-068 removed scan outright (2026-09-29
migration; see [UPSTREAM_TO_ABICHECK.md](../UPSTREAM_TO_ABICHECK.md)). This
one comparison produces the canonical compatibility report — there is one
verdict and one report, not two analyses that can silently disagree. Its
outcome drives:

- the pass/fail gate (`fail-on-breaking`, `fail-on-api-break`);
- the one sticky PR comment (see below);
- the job summary (abicheck's native `add-job-summary`);
- the `abicheck-report` artifact, uploaded with `if: always()` so it is
  available even when the gate fails.

The overall required gate is not this comparison alone: it also includes the
independent coverage contract described in
[Source-depth coverage enforcement](#source-depth-coverage-enforcement)
below, whose own failure can report `NOT_FULLY_EVALUATED` even when the
comparison itself found the change compatible.

## Trusted baseline resolution

The baseline the gate compares against is read directly out of git history at
the PR's exact base SHA (`git show <base-sha>:abi/math.abicheck.json`), not
from the working tree. A PR that edits `abi/math.abicheck.json` in the same
diff that breaks the ABI cannot pass by comparing itself against its own
edited baseline — the trusted side of the comparison always comes from
`main` as it stood before the PR branched. `abi/**` also has a `CODEOWNERS`
entry, so a baseline edit that does land on `main` still needs review.

`since` is likewise pinned to `github.event.pull_request.base.sha` rather
than a moving branch ref, so the analysis is reproducible even if `main`
advances while the PR's checks are running.

`baseline.yml` is the workflow that refreshes `abi/math.abicheck.json` on
pushes to `main`: it builds `//:math`, collects a source-aware ABI snapshot,
normalizes it, and commits it if the normalized content changed. See
[operations.md](operations.md) for the branch-protection and bypass
implications of that direct-to-`main` push.

## The PR comment is abicheck's own `pr-comment`

Only the gating step sets `pr-comment: true`; every diagnostic leg (L2
CastXML/Clang, L4 replay, L4 plugin, strings, math_shared, consumer-scoped,
toolchain matrix) sets `pr-comment: false` and publishes JSON artifacts
only, so there is exactly one sticky comment and it always describes the
gate's own report.

History: while the gate ran `mode: scan`, upstream's comment renderer was a
compare-only no-op, so the lab carried `scripts/render_scan_comment.py`.
Moving the gate to `mode: compare` makes the native renderer apply directly;
the lab renderer was deleted in the same change. The native comment does not
render the lab's independent coverage-contract result — that is surfaced by
the `Enforce gate` step's error, the job summary, and the
`coverage-contract-result` artifact instead (recorded as a gap in
UPSTREAM_TO_ABICHECK.md).

## Source-depth coverage enforcement

A `depth: source` request over this lab's Bazel target frequently can't
actually achieve source-level evidence on its own: abicheck v0.5.0 has no
first-class way to gate on that (the coverage/contract axis that would
produce `COVERAGE_INCOMPLETE` doesn't exist until `abicheck/main`).

`scripts/check_coverage_contract.py` is the lab-side stand-in: a second,
independent gate that reads the same `abicheck-report.json` the gating
compare produced and asserts on its own coverage evidence — Bazel target resolved,
`compile_units >= 1`, export-to-source match ratio `>= 0.95`, public-header
provenance present. Either gate failing fails the PR (`Enforce gate` checks
both `steps.scan.outcome` and `steps.coverage_contract.outcome`); when the
contract isn't met, its result artifact records `analysis_status: INCOMPLETE`,
`compatibility_verdict: NOT_FULLY_EVALUATED`. A missing or unreadable report
fails the contract (and so the gate) closed.

On a compare-schema report the contract reads `layer_coverage` (the same
`{layer, status, detail}` rows scan emitted as `coverage`), takes the
effective depth from `analysis_assurance.effective_depth` and requires
`analysis_assurance.depth_satisfied: true`, and takes public-header
provenance from the report's `scope` block (applied + resolved + not fallen
back), because compare emits no per-check `crosscheck:*` coverage rows.

Two evidence gaps are closed at the workflow level, not just gated red:

- **Public-header provenance** — `abi-scan.yml` passes the `include/`
  directory as the candidate's header root (`new-header: include` on the
  two-sided compare, matching the baseline dump's `public-header-dir:
  include`; `scripts/check_recipe_parity.py` enforces that mapping), turning the provenance-gated crosschecks
  from "skipped" into real evidence.
- **Bazel target resolution** — `abi-scan.yml` runs both `bazel cquery` and
  `bazel aquery` itself; `scripts/build_bazel_evidence_pack.py` combines
  them into one `BuildSourcePack` via `BazelAdapter(cquery=..., aquery=...)`
  and hands it to the gating comparison via `--build-info`.

Both fixes apply to `baseline.yml`'s `dump` too, so both sides of every
comparison have equivalent evidence — but the two workflows react
differently to a cquery/aquery/pip failure in that pipeline, deliberately:

- **`abi-scan.yml`** falls back to the pre-existing (targets-less)
  auto-inference and continues the comparison on degraded evidence — this only
  affects one PR's own coverage-contract result, which stays visible, not
  persisted, so this can only improve evidence, never silently regress it
  below what a PR would have gotten anyway.
- **`baseline.yml`** fails the whole job before `dump`/commit ever run — a
  silent fallback there would get committed to `main` and become every
  future PR's trusted comparison target, so a transient bazel/pip hiccup
  must never produce a degraded baseline.

Export-to-source symbol matching is exempted, not enforced, only when the
changed-source scope is confirmed zero **and** none of the changed files
(via `--changed-files`) can reach the compiler — a positive-evidence
allowlist on both axes, not a default.

The baseline's own dump coverage is validated before it's committed, using
the same `check_coverage_contract.py` script in a mode that reads a `dump`
snapshot's nested `build_source.manifest.coverage` shape instead of a
compare report's top-level `layer_coverage` list.

## Bazel evidence, provenance, and consumer scoping

- **`tools/abicheck/facts.bzl`** — `abicheck_facts_aspect` runs the abicheck
  Clang plugin as its own declared Bazel action output (not an untracked
  side effect of an ordinary compile action), so it participates correctly
  in Bazel's caching. `abicheck_facts_pack` merges per-source-file facts
  across a target and its deps.
- **Producer conformance** — `l2-castxml`, `l2-clang`, `l4-clang-replay`,
  and `l4-clang-plugin` all analyze the same candidate through a different
  producer. `scripts/render_conformance_report.py` renders two pairwise
  agreement reports (L2 CastXML vs. Clang; L4 Clang replay vs. Clang
  plugin), matching findings by `(kind, symbol)`. Neither ever gates.
- **Multi-library aggregate gate** — `strings_lib/` is a second,
  independent target exercising `abicheck aggregate` against a real
  multi-target report set. `abi-scan.yml`'s `aggregate` job builds a
  fail-closed expected-target manifest (`math` required whenever the `scan` job
  judged the PR ABI-relevant); `strings` is deliberately not declared
  required, so its own best-effort compare never gates this job.
- **`cc_shared_library` target shape** — `//:math_shared` is a genuine
  `cc_shared_library` (as opposed to every other target's
  `cc_binary(linkshared = True)`), scanned non-gating by `scan_math_shared`
  against its own baseline (`abi/math_shared.abicheck.json`).
- **Consumer-scoped validation** — `consumer/consumer_app` dynamically links
  `//:math` and calls only a subset of its API. `abicheck compare
  --used-by consumer_app` scopes the comparison to symbols the consumer
  actually imports. Both the consumer and the "old" library are built from
  the PR's base SHA (not HEAD) so the comparison reflects an
  already-shipped binary, not a freshly recompiled one. Non-gating
  (`consumer_scoped` job).
- **Toolchain matrix** — `toolchain_matrix` rebuilds `//:math` under four
  `{gcc, clang-18} × {c++17, c++20}` combinations and compares each against
  the same committed baseline, surfacing any toolchain-dependent divergence.
  Non-gating, watch-only.

## Baseline determinism

`scripts/normalize_baseline.py` strips an explicit allowlist of exact,
verified-volatile paths (timestamps, absolute runner paths, mtimes, per-run
timings) between `dump` and commit, so a baseline refresh only produces a
commit when the public ABI actually changed. It rewrites only fixed schema
field names (`source_location`/`source_header`/`source_path`) from absolute
to repo-relative paths — never anything keyed by arbitrary content (a
function or constant name), which is why it's an explicit path allowlist and
not a key-name pattern.

## Capability matrix and receipts

`capabilities.yaml` is the declarative, machine-checked source of truth for
which validation axis (evidence depth, header frontend, toolchain, target
shape, comparison scope) this repo's CI actually exercises, validated
against real workflow job names by `scripts/check_capability_matrix.py`.
`scripts/capability_receipts.py`/`emit_capability_receipt.py`/
`validate_capability_receipts.py` add a machine-written, per-run receipt for
each `gating: true` capability id, so a required check's presence in the
matrix can't be trusted on its own — the receipt proves that job actually
ran to completion on this run, not just that it exists in the workflow file.

## Performance measurement

`performance.yml` (dispatch + weekly schedule, never on every PR) measures
cold/warm Bazel and abicheck cache speedups and publishes a
`abicheck-lab-performance-timings` artifact. Read-only, never gates.
