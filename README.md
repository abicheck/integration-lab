# ABICheck Bazel Lab

A small public C++ shared-library project used to validate ABICheck GitHub Actions.

- `baseline.yml` builds `//:math` on `main`, collects a source-aware ABI
  snapshot, normalizes it (see below), and commits it to
  `abi/math.abicheck.json` if the normalized content changed.
- `abi-scan.yml` runs on pull requests, builds the candidate with Bazel,
  resolves the trusted baseline from the PR's exact base commit, and runs a
  **single** ABICheck source scan that is simultaneously the merge gate,
  the source of the PR comment, and the job summary (`scan` job). A second,
  non-blocking `canary` job runs the same scan pinned to a fixed
  `abicheck/main` commit instead of the last release, so a PR shows what the
  next abicheck release will do before it ships.

Test PRs intentionally exercise compatible additions, binary ABI breaks,
source/API breaks, and implementation-only changes.

## Gate / PR-comment architecture

The `scan` job runs exactly one gating `abicheck` invocation (`mode: scan`,
`depth: source`, `format: json`, `abicheck-report.json`). Its outcome drives
all of the following, so there is one verdict and one report, not two
analyses that can disagree:

- pass/fail gate (`fail-on-breaking`, `fail-on-api-break`)
- the PR comment (see below)
- the job summary (abicheck's native `add-job-summary`, which does support
  `scan` mode)
- the `abicheck-report` artifact, uploaded with `if: always()` so it is
  available even when the gate fails

The baseline it compares against is read directly out of git history at the
PR's exact base SHA (`git show <base-sha>:abi/math.abicheck.json`), not from
the working tree. A PR that edits `abi/math.abicheck.json` in the same diff
that breaks the ABI cannot pass by comparing itself against its own edited
baseline — the trusted side of the comparison always comes from `main` as it
stood before the PR branched. `abi/**` also has a `CODEOWNERS` entry, so a
baseline edit that does land on `main` still needs review, closing the gap
for a change that only touches `abi/math.abicheck.json` outside a PR that
also changes the library (once branch protection requires code-owner review
on `main` — this repo can declare that requirement, but can't turn it on for
itself).

**Operational prerequisite for whoever does turn that protection on:**
`baseline.yml` pushes its refresh commits directly to `main` (deliberately
unreviewed — it's built from `main`'s own already-merged source, not from a
PR branch; see the `CODEOWNERS` comment). A standard "require a pull
request before merging" rule — the exact setting that gives the
`CODEOWNERS` entries above any effect — blocks *all* direct pushes to
`main`, including this workflow's, unless its actor is explicitly added to
the rule's bypass list (a repository ruleset scoped to the `github-actions`
app or a dedicated bot identity). Turning on branch protection without that
bypass doesn't make baseline refreshes reviewed; it makes them fail
silently, leaving `abi/math.abicheck.json` stale while every later PR keeps
comparing against an old baseline (Codex review).

`since` is likewise pinned to `github.event.pull_request.base.sha` rather
than a moving branch ref, so the analysis is reproducible even if `main`
advances while the PR's checks are running.

### Why the PR comment isn't abicheck's own `pr-comment`

abicheck's built-in sticky PR-comment renderer (`pr-comment: true`) only
activates for `mode: compare` — in both the pinned v0.5.0 release and
current `abicheck/main`, `_maybe_post_pr_comment` in `action/run.sh` is a
silent no-op for `mode: scan`. That's exactly how the *previous* version of
this workflow ended up rendering its PR comment from an unrelated `compare`
call with none of the scan's `sources`/`depth`/`since`/build context — the
comment reported "no L3–L5 evidence" for an analysis that never collected
any, while a *different* analysis (the one with real source evidence) was
actually gating the PR.

Since scan-mode PR comments don't exist upstream yet, `scripts/render_scan_comment.py`
renders a small, literal comment directly from the same `abicheck-report.json`
that gates the PR (verdict, exit code, requested vs. effective depth, and
the evidence-gap `advisories` list abicheck itself emits — the same list
that says things like "Macros, default args, inline/template/constexpr
bodies are off; source-only API changes are not detected"). It's posted/
updated as a sticky comment via `actions/github-script`. This is a
same-repo workaround, not a fix: the real fix is a `scan`-mode PR-comment
renderer upstream in abicheck.

A `compare`-mode step still runs for a detailed binary/header-only diff
(useful for humans who want a full side-by-side), but it's explicitly
non-gating (`fail-on-breaking`/`fail-on-api-break: false`) and its JSON is
published **only** as the `abicheck-diagnostic-compare` artifact — not as a
second PR comment — so it can't be mistaken for the canonical report again.

### Coverage contract: `depth: source` is enforced, not just requested

A `depth: source` request over this lab's Bazel target frequently can't
actually achieve source-level evidence: verified against a real completed
scan on this repo, `abicheck` resolved 0 Bazel targets, matched 0/6
exported symbols back to source, and had no public-header provenance —
yet still reported plain `COMPATIBLE` at exit code 0. abicheck v0.5.0 has
no first-class way to gate on that (the coverage/contract axis,
`--contract-evaluation`/ADR-049, that would produce `COVERAGE_INCOMPLETE`
doesn't exist until `abicheck/main`).

`scripts/check_coverage_contract.py` is the lab-side stand-in: a second,
independent gate that reads the same `abicheck-report.json` the scan
produced and asserts on its own coverage evidence — Bazel target
resolved, `compile_units >= 1`, export-to-source match ratio
`>= 0.95`, public-header provenance present. Either gate failing fails the
PR (`Enforce gate` checks both `steps.scan.outcome` and
`steps.coverage_contract.outcome`); when the contract isn't met, the PR
comment shows `analysis_status: INCOMPLETE`, `compatibility_verdict:
NOT_FULLY_EVALUATED`, and exactly which requirement failed — as its own
clearly-labeled section, never merged into abicheck's own verdict line
above it.

**Update: two of the three evidence gaps above are now actually closed at
the workflow level**, not just gated red and documented:

- **Public-header provenance** — `abicheck` v0.5.0 supports
  `--public-header-dir` on `scan`/`dump` at the CLI level; it just isn't a
  typed input on the v0.5.0 Action yet. `abi-scan.yml` passes it through
  `extra-args: '--public-header-dir include'`, which is what turns the
  four provenance-gated crosschecks from "skipped: no public-header
  provenance" into real evidence.
- **Bazel target resolution** — v0.5.0's zero-config `--sources` path only
  ever auto-runs `bazel aquery` (never `cquery`), and `BazelAdapter`'s
  cquery-only code path is the *only* one that ever populates
  `BuildEvidence.targets` — verified against abicheck's own source, not
  guessed. `abi-scan.yml` now runs both `bazel cquery` and `bazel aquery`
  itself, then `scripts/build_bazel_evidence_pack.py` combines them into
  one `BuildSourcePack` (abicheck's own on-disk evidence format) via
  `BazelAdapter(cquery=..., aquery=...)` directly — the same combination
  the CLI's now-removed `collect --from bazel-cquery=... --from
  bazel-aquery=...` used to wire up — and hands it to the gating scan via
  `--build-info`. Fails closed and falls back to the pre-existing
  (targets-less) auto-inference on any problem, so this can only improve
  evidence, never regress it.

Both fixes apply to `baseline.yml`'s `dump` too, not just the gating
`scan` — an earlier version of this fix only wired the PR-side scan,
leaving the *baseline*'s old side with degraded evidence (0 targets, no
provenance) while the candidate side got full evidence. That asymmetry
would have let a source-only API change (a macro, a template, an inline
body) slip through: the coverage contract only asserts on the candidate's
own evidence completeness, so it can't by itself tell you the *old* side
was never fully captured (Codex review). `baseline.yml` now runs the same
cquery/aquery/evidence-pack pipeline before `dump`, so both sides of every
comparison have equivalent evidence going forward.

One deliberate difference from the scan job: on `baseline.yml` this
pipeline is **gating, not best-effort**. The scan job's version falls back
to degraded auto-inference on any problem, because that only affects one
PR's own coverage-contract result — visible, not persisted. A silent
fallback in `baseline.yml` would instead get *committed to main* and
become every future PR's trusted comparison target, so a transient
bazel/pip hiccup could permanently downgrade the baseline with no visible
signal (Codex review). If Bazel-cquery/aquery capture, the pip install, or
the pack build fails, the whole job fails before `dump`/commit ever run —
the old (better, or at least previously-accepted) baseline stays in place
rather than being silently overwritten with a worse one.

**Export-to-source symbol matching is exempted, not enforced, when there's
nothing to match — and only when the diff genuinely couldn't have changed
compiled semantics.** `depth: source` defaults to `scope=changed` replay,
so a PR that doesn't touch any C++ source genuinely has zero compile units
in scope: `check_coverage_contract.py` reads `L4_source_abi`'s own "P/S
TUs parsed" count and considers exempting the `>= 0.95` ratio requirement
when the *selected* count is confirmed zero (verified against a real
report from exactly this scenario: "scope=changed, 0/0 TUs parsed, 0/6
symbols matched"). But an empty changed-source scope alone isn't enough:
a PR that changes `BUILD.bazel`, a `.bzl` file, or `.bazelrc` could alter
compiled semantics (a new `-D` define flipping `#ifdef`-guarded
declarations) without touching a single `.cc`/`.h` file, so the exemption
also requires the full changed-file list (passed via `--changed-files`)
and confirms none of those paths can reach the compiler at all — this
repo's own gate-only maintenance files (workflows, `scripts/*.py`,
`README.md`, `CODEOWNERS`) qualify; `BUILD.bazel`/`*.bzl`/`.bazelrc`/
`include/`/`src/`/`abi/` never do (Codex review). Either signal missing or
ambiguous falls through to the ordinary ratio check — exemption is a
positive-evidence allowlist on *both* axes, not a default. A PR that
actually changes `src/`/`include/` gets real symbols in scope and the
ratio is enforced normally, now with compile units correctly linked to
their resolved Bazel targets.

**The baseline's own dump coverage is validated before it's committed,
not just its input evidence.** A good Bazel cquery/aquery capture only
proves abicheck was *handed* good evidence — the `dump` itself can still
degrade downstream while exiting 0 regardless, the same abicheck v0.5.0
behavior the whole coverage contract exists to catch on the scan side
(Codex review). `check_coverage_contract.py` is reused against the raw
`dump` output before `normalize_baseline.py`/commit run, with
`--no-require-public-header-provenance` (dump mode never runs crosschecks
at all -- there's no "other side" to cross-check against, so the four
provenance-gated `crosscheck:` layers this script looks for are
structurally absent from every dump) and no `--changed-files` (dump
always processes the whole tree, so the empty-scope exemption doesn't
apply and shouldn't be reachable). This required generalizing
`_coverage_by_layer` to read either shape: a `scan` report's top-level
`coverage` list, or a `dump` snapshot's nested
`build_source.manifest.coverage` (no top-level `coverage`/`level` key at
all) — verified against the real committed baseline, which has the
identical `{layer, status, detail}` shape just one level deeper.

This was verified as far as static analysis of the pinned commit allows —
the actual effect (does `bazel_targets` really read > 0, does the
provenance crosscheck really flip to `present`) is confirmed against real
CI runs on this repo's own PRs, same as every other claim in this
document. The `canary` job's `--contract-evaluation` run against
`abicheck/main` remains useful alongside this: it shows whether upstream's
own fix, once it ships, agrees with this lab-side check's verdict.

Every field `check_coverage_contract.py` reads was verified against a real
downloaded scan report, not guessed — see its module docstring for the
exact evidence and why the `L3_build`/`L4_source_abi` free-text `detail`
parsing is documented as fragile (fails closed, not silently trusting, if
the format ever stops matching).

## Baseline determinism

Raw `abicheck --mode dump` output embeds run-specific noise: wall-clock
timestamps, absolute runner paths (`/home/runner/work/...`,
`/home/runner/.cache/bazel/...`), filesystem mtimes, per-run extractor
timings, and content-hash fields computed over that volatile payload before
this script ever sees it. Left in place, every baseline refresh would
produce a commit even when the public ABI didn't change.

`scripts/normalize_baseline.py` runs between `dump` and the commit step.
It deletes an **explicit allowlist of exact paths** (`DELETE_PATHS`), each
verified against the real committed `abi/math.abicheck.json` before being
added — not a key-name pattern applied broadly. Two earlier attempts at a
broader pattern both turned out to delete real ABI content: a name-keyed
map's *key* is itself arbitrary content (a real constant or function could
legitimately be called `timeout_s` or `elapsed`; `build_source`'s own
source-linkage evidence — `mappings`, `source_edges`, `indexes.by_target`/
`by_file`/..., preprocessor `defines` — is keyed by real file paths, symbol
names, and target labels, not just provenance), so no subtree of the
document is safe to filter by key-name pattern. The exact paths currently
stripped: the document-root `created_at`/`version`/`git_commit` (the
latter two because `baseline.yml` passes `new-version:
main-${{ github.sha }}`, which would otherwise change on every push
regardless of whether the ABI did — git history on the file already
records which commit each refresh reflects), `build_source_pack
.content_hash`, and inside `build_source`: `manifest.created_at`,
`manifest.artifacts`, each extractor's `artifacts`, each coverage entry's
`elapsed_s`, `source_abi.coverage`'s `cache_lookup_s`/`extract_s`/
`link_s`/`elapsed_s`, and `source_graph.graph_id`. A future abicheck
version adding a new volatile field elsewhere shows up as a small amount
of visible, debuggable baseline noise rather than a silently deleted ABI
entity — that asymmetry is deliberate.

Absolute-path rewriting (repo checkout → repo-relative,
Bazel execroot/output → `bazel-out/...`-relative) is scoped the same way:
only `source_location`/`source_header`/`source_path`, fixed schema field
names of function/type descriptors that are never the key of a
name-keyed content map, so matching them by name can't collide with real
content the way `DELETE_PATHS`-style stripping could. No other absolute
paths that vary run-to-run have been found anywhere else in the tree; the
only other absolute paths present (`compile_units[].argv[0]`,
`link_units[].linker_argv[0]`, e.g. `/usr/bin/gcc`) are stable toolchain
paths, not noise, and are left untouched. `<N>s` timing fragments in the
extractor `detail` field (also a fixed schema field name) are collapsed to
a constant placeholder.

Verified idempotent (double-normalizing is a no-op) against the committed
baseline, and against a synthetic report exercising every collision this
design guards against (a `constants.timeout_s` entry, a function named
`elapsed_s`, a `source_graph.indexes.by_target` key containing `timeout_s`,
a `defines` map with `timeout_s`/`elapsed`, and path-like constant values)
— all survive `normalize()` unchanged while the real volatile fields
listed above are still stripped.

## Build environment

`.bazelversion` and `.bazelrc` pin the Bazel version and an explicit
`-std=c++17`, so a compiler/standard bump can't silently change name
mangling/ABI without touching anything this repo's own path filters watch.
Both `abi-scan.yml` and `baseline.yml` cache Bazel's disk cache
(`~/.cache/bazel-disk`) via `actions/cache`, keyed on
`MODULE.bazel`/`.bazelversion`/`.bazelrc`/`BUILD.bazel`.

**Not yet added:** a `MODULE.bazel.lock`. Generating one correctly needs a
real `bazel mod deps --lockfile_mode=update` run, which isn't available
while authoring these files outside CI — tracked as a follow-up rather than
hand-authored here.

## Producer conformance reports

The three non-gating L2/L4 profile diagnostics (`l2-castxml`, `l2-clang`,
`l4-clang-replay`, plus the separate best-effort `l4-clang-plugin` job) all
analyze the same candidate library/header/baseline through a different
producer. `abi-scan.yml` now also renders two conformance reports
comparing sibling producers pairwise, via `scripts/render_conformance_report.py`:

- **L2 · CastXML vs. Clang** — do the two header-AST frontends agree on
  this library's public surface? Rendered as a step in the `scan` job
  (both artifacts are local there) and uploaded as `abicheck-conformance-l2`.
- **L4 · Clang replay vs. Clang plugin** — do the two L4 source-fact
  producers agree? Rendered in its own `conformance` job (`needs: [scan,
  l4_clang_plugin]`, `if: always()`, since `l4_clang_plugin` is best-effort
  and may not run) and uploaded as `abicheck-conformance-l4`.

Each report matches findings by `(kind, symbol)` across the two reports
(`compare`- and `scan`-mode report shapes are both handled — see the
script's own docstring), and flags: findings present in only one side,
and old/new value text that differs on an otherwise-matched finding (which
may be a genuine cross-backend type-spelling difference, e.g. CastXML's
`char const*` vs. Clang's `char const *`, not necessarily a bug). Both
reports post to the job summary; neither ever gates. A missing side (e.g.
`l4_clang_plugin` skipped on this runner) degrades to a labeled "skipped"
section rather than failing.

## Known limitations / follow-ups

This lab currently validates one `cc_library` + `cc_binary(linkshared =
True)` target with a single header-only public surface. It does not yet
cover: `cc_shared_library`, generated headers, multiple libraries, a
consumer binary, a `MODULE.bazel.lock`, or a machine-readable scenario
matrix with an expected/actual oracle per patch (fixtures/patches/scripts
that apply a change to a clean fixture, run a scan, and assert on the JSON
— rather than relying on long-lived PRs and human eyeballing of comments).
A security scenario specifically demonstrating "breaking code change +
modified committed baseline in the same PR still gates red" also hasn't
been exercised as an actual test PR yet — the architecture above (baseline
read from base SHA, `abi/**` in `CODEOWNERS`) should already guarantee it
structurally, but that's a claim, not yet a verified test. These are
tracked as follow-up work rather than folded into this change.

## Scenario validation (`scenarios.yml`)

`abi-scan.yml`'s gate only ever exercises whatever a given PR's diff to
`//:math` happens to be -- it never actually proves abicheck detects a
real break, or correctly leaves a compatible change alone. `scenarios.yml`
closes that gap: `fixtures/<name>/{v1,v2}/` are small, independent Bazel
targets (never touching `//:math`), and `scenarios/manifest.yaml` is a
machine-readable oracle pairing each one with its expected verdict.
`scripts/run_scenario.py` builds every `v1`/`v2` pair, runs `abicheck
compare` between them, and fails if the actual verdict doesn't match the
manifest.

Current scenarios:

| Scenario | Change | Expected verdict |
|---|---|---|
| `add_function` | v2 adds a new exported function | `COMPATIBLE` |
| `remove_function` | v2 removes an exported function | `BREAKING` |
| `change_signature` | v2 changes a parameter type (mangled-name change) | `BREAKING` |

Run locally: `python3 scripts/run_scenario.py` (needs `bazel` and the
`abicheck` CLI on `PATH`, and `pyyaml` installed) — or `--only <name>` for
a single scenario. Results are written to `scenario-results/summary.json`.

### Automatic stale-suppression detection (`scenarios-canary.yml`)

`scenarios.yml` re-verifies every scenario's expected verdict and every
suppression's `expected_suppressed_count`/`expected_suppressed_symbols`
on every run — but only ever against the one pinned `abicheck` commit,
and only when this repo's own fixtures/scenarios/suppressions change. A
suppression can go stale for a reason that has nothing to do with a
change in *this* repo: an upstream `abicheck` release changes how a
finding is classified or matched, and a selector that used to match a
real finding silently stops. `scenarios-canary.yml` closes that gap: the
identical scenario suite, run against `abicheck/main`'s current HEAD on a
weekly schedule (plus `workflow_dispatch` for an on-demand check right
before a planned pin bump) — so staleness is caught independently of any
lab-repo PR activity, before the pin is ever bumped to include it. Never
gates a PR (nothing here triggers it) and posts no PR comment (there is
none); a red run in the Actions tab plus its own job summary is the
signal, and it means "review before bumping the pin" (which may be a
desired detector improvement, not necessarily a bug), not "this repo is
broken."

**Not yet covered** (explicitly out of scope for this initial slice, not
silently dropped): a `cc_shared_library` scenario, a generated-header
scenario, a multi-library/consumer-app scenario, `MODULE.bazel.lock`
pinning, and a benchmark workflow. These are real follow-up work, not
abandoned scope.
