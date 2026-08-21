# Operations

Operational requirements and prerequisites for running or maintaining this
lab's CI, aimed at repository maintainers and whoever administers branch
protection.

## Baseline refresh behavior

`baseline.yml` runs on pushes to `main`: it builds `//:math` (and
`strings`/`math_shared`), collects a source-aware ABI snapshot, normalizes
it (`scripts/normalize_baseline.py`), and **commits directly to `main`** if
the normalized content changed. This is deliberate and unreviewed by
design — it's built from `main`'s own already-merged source, not from a PR
branch — but see the branch-protection implication immediately below.

## Branch-protection implications

**Operational prerequisite for whoever turns on branch protection:**
`baseline.yml` pushes its refresh commits directly to `main`. A standard
"require a pull request before merging" rule blocks *all* direct pushes to
`main`, including this workflow's, unless its actor is explicitly added to
the rule's bypass list (a repository ruleset scoped to the `github-actions`
app or a dedicated bot identity). Turning on branch protection without that
bypass doesn't make baseline refreshes reviewed — it makes them fail
silently, leaving `abi/math.abicheck.json` stale while every later PR keeps
comparing against an old baseline.

## Required checks

The only status check that should ever be marked **required** in branch
protection today is `.github/workflows/abi-scan.yml`'s `scan` job (and,
where enabled, `verify_capability_receipts`). `integration-shadow.yml`'s
`integration_gate` job is real and can fail, but is **not** required —
adding it to the required list today would be a policy change beyond the
scope of this repository's current promotion state; see
[roadmap.md](roadmap.md) for the planned path there.

## CODEOWNERS

`.github/CODEOWNERS` protects the trust boundary around the canonical gate:
`abi/**` (the committed baselines), the gating workflows themselves
(`baseline.yml`, `abi-scan.yml`, `scenarios.yml`), the composite actions
they call (`.github/actions/skip-check`, `.github/actions/resolve-baseline`),
and the scripts that implement coverage/receipt enforcement
(`normalize_baseline.py`, `check_coverage_contract.py`,
`render_scan_comment.py`, `paths_changed.py`, `build_bazel_evidence_pack.py`,
`capability_receipts.py`, `emit_capability_receipt.py`,
`emit_scenario_receipts.py`, `validate_capability_receipts.py`), plus
`CODEOWNERS` itself. Each entry exists because an unreviewed edit to that
path could make the required gate pass without the trust property it's
supposed to enforce actually holding — see the comments in that file for
the specific reasoning per path. **This only has teeth once branch
protection on `main` requires code-owner review for matching paths.**

## GitHub Actions permissions

Workflows in this repo request the minimum permissions their job needs
(typically `contents: read`; `abi-scan.yml`'s comment-posting steps need
`pull-requests: write`; `baseline.yml` needs `contents: write` to commit).
Check each workflow's own `permissions:` block before widening it.

## Cache behavior

Both `abi-scan.yml` and `baseline.yml` cache Bazel's disk cache
(`~/.cache/bazel-disk`) via `actions/cache`, keyed on
`MODULE.bazel`/`.bazelversion`/`.bazelrc`/`BUILD.bazel` (plus each job's own
additional relevant `BUILD.bazel` files, e.g. `strings_lib/BUILD.bazel` for
`scan_strings`). `MODULE.bazel.lock` is committed and enforced via
`.bazelrc`'s `common --lockfile_mode=error`, so an unreflected
`MODULE.bazel` change fails closed with an explicit message rather than
silently re-resolving whatever the registry currently serves.

## Artifact retention

Diagnostic and report artifacts (`abicheck-report`,
`abicheck-diagnostic-compare`, conformance reports, per-profile staged
build output, profile receipts, profile reports) are retained for 14–30
days depending on the workflow (see each workflow's `retention-days`).
`performance.yml`'s timing artifact is retained 90 days — long enough to
eyeball a trend by hand across runs.

## Scanner pinning

`abi-scan.yml` installs a specific pinned `abicheck` release
(`pip install abicheck @ git+https://github.com/abicheck/abicheck.git@...`).
`scenarios-canary.yml` runs the same scenario suite against
`abicheck/main`'s current HEAD instead, specifically so a pin bump can be
evaluated against real scenario/suppression behavior before it's made.
Bumping the pin is a deliberate, reviewed action — check the canary's
latest run first.

## Long-lived intentional test PRs

Some pull requests in this repository exist specifically to exercise a
compatible addition, a binary ABI break, a source/API break, or an
implementation-only change against the real gate, and are intentionally
kept open as reusable acceptance cases rather than merged or closed. Do not
treat an old, open PR with a "breaking" or "compatible" title as
abandoned work — check whether it's referenced as a scenario or test case
before closing it.
