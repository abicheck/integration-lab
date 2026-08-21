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

## Multi-build-system profile baseline lifecycle

Two more baseline channels exist alongside `baseline.yml`'s own (the
canonical `abi/math.abicheck.json`/`abi/strings.abicheck.json`/
`abi/math_shared.abicheck.json`, unchanged by either):

- **Accepted-main channel (`profile-baseline.yml`).** Runs on pushes to
  `main`: one matrix leg per profile (`refresh`, read-only, `continue-on-
  error: true`) builds and `ci/check_profile.py dump`s that profile's
  own `math`/`strings` baseline into an artifact; a single fan-in
  `commit` job (the only job in this workflow with `contents: write`)
  downloads every profile's artifact and runs `ci/apply_profile_baselines.py`
  once, which validates each raw dump (well-formed, correct `kind`/
  `profile_id`/`target`, non-empty `dynamic_symbols`/`public_headers`)
  and only writes `abi/profiles/<id>/<target>.abicheck.json` over its
  existing committed content when the dump is both valid AND actually
  different — an invalid or missing dump for one profile/target leaves
  that file exactly as it was and never blocks any other profile/target's
  own refresh in the same run. Same trust model as `baseline.yml`: a
  direct, unreviewed push, built from `main`'s own already-merged source
  — see [CODEOWNERS](#codeowners) below.
- **Release-contract channel (`release.yml`).** Runs on `release:
  published`: for every `contract: true` profile (`ci/select_profiles.py
  --event release --contract-only` — today, exactly
  `linux-x86_64-gcc14-cxx17-bazel`), rebuilds that profile from the
  release's own target commit and re-dumps it, then requires
  (`ci/apply_profile_baselines.py --verify-only`) that the fresh dump
  matches what's already committed at that exact commit byte-for-byte —
  a real integrity check, not a formality; a mismatch fails the job and
  nothing is published. Once verified, publishes
  `abicheck-baseline-<profile-id>-{math,strings,manifest}` as immutable
  release assets (`ci/emit_release_manifest.py` writes the manifest:
  release tag, source commit, a `generation` number — this workflow run's
  own `github.run_attempt` for this tag — profile id, and a sha256 digest
  per baseline file). Idempotent: an asset name that already exists on
  the release is compared by digest first — identical content is treated
  as a safe retry (skipped, not re-uploaded), different content fails the
  job closed rather than silently overwriting a published asset.

Both are still part of the advisory multi-build-system system — neither
is a new required gate; see
[integration-profiles.md](integration-profiles.md).

## Branch-protection implications

**Operational prerequisite for whoever turns on branch protection:**
`baseline.yml` pushes its refresh commits directly to `main`. A standard
"require a pull request before merging" rule blocks *all* direct pushes to
`main`, including this workflow's, unless its actor is explicitly added to
the rule's bypass list (a repository ruleset scoped to the `github-actions`
app or a dedicated bot identity). Turning on branch protection without that
bypass doesn't make baseline refreshes reviewed — it makes them fail
silently, leaving `abi/math.abicheck.json` stale while every later PR keeps
comparing against an old baseline. `profile-baseline.yml`'s own `commit`
job pushes directly to `main` the identical way, for the identical reason
— it needs the identical bypass, or its own refreshes fail silently the
same way.

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
`abi/**` (the committed baselines — this also covers
`abi/profiles/**`, the multi-build-system profile baselines
`profile-baseline.yml`/`release.yml` read and write), the gating
workflows themselves (`baseline.yml`, `abi-scan.yml`, `scenarios.yml`),
the composite actions they call (`.github/actions/skip-check`,
`.github/actions/resolve-baseline`), and the scripts that implement
coverage/receipt enforcement (`normalize_baseline.py`,
`check_coverage_contract.py`, `render_scan_comment.py`,
`paths_changed.py`, `build_bazel_evidence_pack.py`,
`capability_receipts.py`, `emit_capability_receipt.py`,
`emit_scenario_receipts.py`, `validate_capability_receipts.py`), plus
`CODEOWNERS` itself. The same reasoning extends to the two write-capable
multi-build-system workflows: `profile-baseline.yml`/`release.yml`
themselves, and the scripts they trust to decide what gets committed or
published (`ci/apply_profile_baselines.py`, `ci/emit_release_manifest.py`,
`ci/select_profiles.py` — the last one specifically because
`release.yml` trusts its `--contract-only` filtering to decide which
profile(s) are trusted enough to get a release-contract baseline at all).
Each entry exists because an unreviewed edit to that path could make the
required gate — or, for the two newer workflows, the write/publish path
itself — succeed without the trust property it's supposed to enforce
actually holding — see the comments in that file for the specific
reasoning per path. **This only has teeth once branch protection on
`main` requires code-owner review for matching paths.**

## GitHub Actions permissions

Workflows in this repo request the minimum permissions their job needs
(typically `contents: read`; `abi-scan.yml`'s comment-posting steps need
`pull-requests: write`; `baseline.yml` needs `contents: write` to commit).
`profile-baseline.yml`/`release.yml` both default to `contents: read` at
the workflow level and grant `contents: write` only on the one job (and,
implicitly, only for the one step) that actually pushes or publishes —
every other job/step in either workflow stays read-only. Check each
workflow's own `permissions:` block before widening it.

## Cache behavior

Both `abi-scan.yml` and `baseline.yml` cache Bazel's disk cache
(`~/.cache/bazel-disk`) via `actions/cache`, keyed at minimum on
`MODULE.bazel`/`.bazelversion`/`.bazelrc`/`BUILD.bazel`. `baseline.yml` uses
that same key for every job it runs, including its `strings`/`math_shared`
dumps. `abi-scan.yml` widens the key per job where relevant — e.g. adding
`strings_lib/BUILD.bazel` for `scan_strings`, or `consumer/BUILD.bazel` for
`consumer_scoped` — so a change to a target-specific `BUILD.bazel` only
invalidates the cache for jobs that actually depend on it. `MODULE.bazel.lock`
is committed and enforced via
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
(`pip install "abicheck @ git+https://github.com/abicheck/abicheck.git@..."`
— quoted as one argument, matching the workflow's own invocation).
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

## Repository transfer readiness checklist

The repository already lives at `abicheck/integration-lab`; this section
is the checklist for whoever moves it again (a fork consolidation, an org
rename, or an actual GitHub "Transfer ownership") without silently
breaking a hard-coded assumption. None of these items *perform* a
transfer — see the top-level design doc's explicit non-goal against
transferring a repository as part of a code-only PR.

- [ ] `ci/emit_build_output.py`'s `project.name` and
  `scripts/merge_abicheck_facts.py`'s `created_by` string are cosmetic
  provenance labels, not comparability inputs — update them to match
  the new repository slug, but they do not need to match any git remote
  at runtime.
- [ ] `scripts/normalize_baseline.py --repo-root-marker` defaults to the
  checkout directory name (`integration-lab`) so absolute paths recorded
  during `dump` get stripped consistently. If the repository directory
  name changes (a rename, not just an org move), update the default and
  regenerate every committed baseline
  (`abi/*.abicheck.json`, `abi/profiles/*/*.abicheck.json`) — a stale
  marker doesn't break comparability, it just leaves host-specific
  absolute-path fragments un-stripped in freshly generated snapshots.
- [ ] Grep for the literal old slug — both `<old-org>/<old-repo>` (e.g.
  `abicheck/bazel-lab`) AND the bare old repo name by itself (e.g.
  `abicheck-bazel-lab`, which appears with no org prefix in `ci/schemas/
  *.json`'s own `$id`/`title` fields) — before closing out a transfer:
  ```
  grep -rln '<old-org>/<old-repo>' . ; grep -rln '<old-repo-bare-name>' .
  ```
  Replace both placeholders with this repository's actual previous
  identity before running. Either command returning anything outside
  this file's own historical example above and `UPSTREAM_TO_ABICHECK.md`
  (which intentionally keeps the lab revision it audited under its
  original name for traceability) is a real stale reference, not
  history — fix it.
- [ ] Branch protection and required status checks (currently the Bazel
  `abi-scan.yml` gate; see `docs/canonical-bazel-gate.md`) are GitHub
  repository settings, not files — they do not migrate automatically on
  a transfer and must be re-verified against the destination repository
  afterward.
- [ ] `.github/CODEOWNERS`, unlike branch protection, IS a tracked file
  that migrates with the repository — a transfer to an organization
  where the current owners (`@napetrov`, throughout that file) lack
  write access leaves every protected path (`abi/**`, workflow files,
  etc. — see that file itself) without an eligible reviewer, silently
  defeating required code-owner review rather than erroring. Update its
  owners as part of the transfer, not after, and confirm each still has
  write access to the destination repository.
- [ ] `canary.yml`'s `repository_dispatch: {types: [scanner-candidate]}`
  trigger is the receiver side only — nothing in *this* repository sends
  that event (confirmed: `release.yml` has no `repository_dispatch` step
  at all). The sender lives in the upstream `abicheck/abicheck` workflow
  and targets this repository's current `owner/repo` explicitly; that
  upstream dispatch target is what needs updating when this repository's
  slug changes, not anything on this side.
- [ ] README badges and any status-check links that hard-code the
  current `owner/repo` in their URL need updating alongside the actual
  transfer, not before — updating them early just points a badge at a
  repository that doesn't exist yet.
