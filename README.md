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

## ABICheck Integration Lab: multi-build-system profiles (in progress)

This repo's real ABI gate has, until now, only ever exercised one build
system: the root Bazel build (`.bazelrc`, `MODULE.bazel`, `BUILD.bazel`).
A new, deliberately staged effort is adding a **profile** concept -- a
named (build system x compiler x C++ standard) combination, declared once
in `ci/profiles.yaml`, that any tooling (a CI workflow, a local script)
can build, stage, and eventually scan uniformly, instead of every new
build system needing its own hand-written job from scratch.

**PR1 (this change) adds three profiles and proves each one builds the
same source cleanly and stages canonical output -- it does NOT run any
ABICheck scan against the two new build systems, and it does NOT change
what gates a PR:**

- `linux-x86_64-gcc14-cxx17-bazel` -- the existing, unchanged root Bazel
  build. `contract: true` in `ci/profiles.yaml`: this is the one profile
  whose result is (still, exactly as before) trusted for anything beyond
  "did it build" -- `abi-scan.yml`'s `scan` job remains the sole required
  ABI gate and doesn't read `ci/profiles.yaml` at all.
- `linux-x86_64-gcc14-cxx17-cmake-ninja` -- a real CMake+Ninja build
  (`buildsystems/cmake/`) of the identical `include/`, `src/`,
  `strings_lib/`, `consumer/` sources, referenced directly rather than
  copied. `contract: false`: advisory/shadow only.
- `linux-x86_64-gcc14-cxx17-make-bear` -- a handwritten Makefile build
  (`buildsystems/make/`) of the same sources, with an optional
  `bear`-generated `compile_commands.json` when `bear` is on `PATH`.
  `contract: false`: advisory/shadow only.

`.github/workflows/integration-shadow.yml` is the new, entirely
non-gating workflow that builds and stages all three on every PR and push
to `main`: `ci/select_profiles.py` resolves which profiles a given event
runs (from `ci/profiles.yaml` + `ci/event-policy.yaml`), `ci/run_profile.py`
drives one profile's `ci/backends/{bazel,cmake,make}.py` backend through a
real build, and `ci/emit_build_output.py` stages the result into a
canonical `abicheck-build-<profile-id>/` directory (`build-output.json`,
validated against `ci/schemas/build-output.schema.json`, plus staged
`artifacts/`, `headers/`, `evidence/`, `provenance/`). Every per-profile
step runs `continue-on-error: true`, and this workflow is never added to
branch-protection required status checks -- a CMake or Make build failure
can never block a merge; only `abi-scan.yml`'s own Bazel gate still can.

See `ci/profiles.yaml` for the full profile manifest (including an honest
note on this lab's actual local/CI gcc-14 availability), `buildsystems/cmake/`
and `buildsystems/make/` for the two new build definitions, and
`UPSTREAM_TO_ABICHECK.md`'s 2026-08-21 entry for a build-system-identity
gap this staging work surfaced upstream. Wiring an ABICheck scan into the
CMake/Make profiles, and deciding whether either should ever become
`contract: true`, is explicitly out of scope for this PR.

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

`MODULE.bazel.lock` (committed, `module-bazel-lock-pinning` in
`capabilities.yaml`) pins every Bazel Central Registry module this
workspace transitively resolves — regenerated with a real
`bazel mod deps --lockfile_mode=update` run, not hand-authored. `.bazelrc`'s
`common --lockfile_mode=error` enforces it against every Bazel invocation
in every job (build, query, cquery, aquery, mod), not just one — a
`MODULE.bazel` change left unreflected in the committed lock fails
closed with an explicit "run `bazel mod deps --lockfile_mode=update`"
message rather than silently re-resolving whatever the registry
currently serves. Verified directly: a deliberately mismatched
`MODULE.bazel` (a downgraded `rules_cc` version not in the committed
lock) fails `bazel build //:math` with exactly that message; reverting
it builds cleanly again. To intentionally update the lock (e.g. after
bumping a `bazel_dep` version), regenerate it explicitly and commit the
result — a later `--lockfile_mode` flag on the command line overrides
`.bazelrc`'s default.

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
`char const*` vs. Clang's `char const *`, not necessarily a bug). A
scan-mode finding never carries old/new at all, so a pairing where either
side is scan-shaped (the L4 pair) matches by `(kind, symbol)` only, with
an explicit note rather than a false "value mismatch". A verdict outside
abicheck's own conclusive set (`NO_CHANGE`/`COMPATIBLE`/
`COMPATIBLE_WITH_RISK`/`API_BREAK`/`BREAKING` — e.g. `NOT_COMPARABLE`,
`BUDGET_OVERFLOW`, an error) means that side's run didn't reach a real
conclusion, so a matching (or even empty) findings list on both sides
never reads as a green "fully agree" in that case — an incomplete or
failed run is producer *silence*, not producer *agreement*. Both reports
post to the job summary; neither ever gates. A missing side (e.g.
`l4_clang_plugin` skipped on this runner) degrades to a labeled "skipped"
section rather than failing.

**Compiler consistency for the L4 pair:** `l4-clang-plugin` must be
built with Clang (the plugin injects via `-Xclang`), so `l4-clang-replay`'s
own candidate is *also* rebuilt with `CC=clang-18`/`CXX=clang++-18` right
before that leg runs (after the required gate and both L2 legs have
already consumed the original gcc-built binary) — otherwise
compiler-dependent symbols/DWARF/type representations could read as a
producer-only divergence even when the two producers genuinely agree,
defeating the point of isolating the producer axis. The Bazel evidence
pack feeding L4 source facts is re-captured (a second `cquery`/`aquery` +
`build_bazel_evidence_pack.py` run) under the same clang-18 environment
too, so `l4-clang-replay`'s recorded compile actions describe the same
toolchain as the binary it's scanning, not the required gate's original
gcc build. Best-effort like the plugin job's own Clang install: a
failure anywhere in this chain just skips the `l4-clang-replay`
diagnostic for that run.

## Declared-output source-fact collection (`tools/abicheck/facts.bzl`)

The `l4-clang-plugin` profile used to inject the abicheck Clang plugin via
`--copt` onto `//:math`'s own ordinary compile action -- from Bazel's point
of view that action's only output was the object file, so the plugin's
`source_facts/*.jsonl` write was an untracked *side effect* invisible to
Bazel's caching machinery (local, disk, or remote): a cache hit on that
exact compile action would silently skip invoking clang -- and therefore
the plugin -- entirely, leaving the facts directory empty while the build
still reported success. The `l4_clang_plugin` job's own fix at the time
was to simply never consult a disk cache for this job at all.

`tools/abicheck/facts.bzl`'s `abicheck_facts_aspect` closes this
structurally instead: for every compiled source in a `cc_library`/
`cc_binary`, it runs the plugin as its *own*, separate action (a second
`-fsyntax-only` invocation of the same compiler, built from the target's
real `CcInfo` compilation context) with the resulting `source_facts/`
directory declared via `ctx.actions.declare_directory` -- a real Bazel
action output like any other, so it now participates in Bazel's ordinary
caching contract correctly. `abicheck_facts_pack` (the same file) merges
every per-source-file directory across a target and its `deps` into one
`abicheck_inputs/`-shaped pack (per-TU filenames already embed a
source-content hash, so merging is a safe flatten). `//tools/abicheck:math_abicheck_inputs`
(root `BUILD.bazel`) is this repo's own wiring of it, and
`.github/workflows/abi-scan.yml`'s `l4_clang_plugin` job now just builds
that target directly (`bazel build //:math //tools/abicheck:math_abicheck_inputs`)
instead of hand-composing `--copt` flags, with a disk-cache step enabled
the same way the `scan`/`canary` jobs already have one.

**A second, independent fix was required, not just the declared-output
change:** even with a correctly-matching explicit `public-roots=`, the
facts action still produced zero declarations, because the plugin gates
every declaration on `SourceManager::isInSystemHeader()` *before* ever
testing it against `public-roots` -- and this repo's own `:math_api`
target declares its public header directory via `cc_library`'s
`includes = [...]` attribute, which Bazel always renders as `-isystem`
(verified empirically: a manual `clang -H` comparison across `-I` vs.
`-isystem`, and across relative vs. absolute search-directory forms,
traced through to the plugin's own `isInSystemHeader` call sites in
`AbicheckFactsPlugin.cpp`). The aspect's facts-collecting action therefore
builds its command line with `system_includes` folded into
`include_directories` (`-I`, not `-isystem`) for this one action only --
the target's real compile action is untouched, and this reclassification
only removes the system-header disqualification for directories the
caller already names in `public_roots`, so it cannot broaden the public
surface to some unrelated third-party `-isystem` directory. See the
`.bzl` file's own module docstring for the full design notes.

The plugin's built `.so` is staged at `tools/abicheck/libabicheck-facts.so`
(gitignored -- it's a compiled artifact, ABI-locked to the loading clang's
exact LLVM major, never committed) by the `l4_clang_plugin` job's existing
"Build the Clang plugin" step before `abicheck_facts_aspect` runs; outside
CI, build it locally with the recipe in
`contrib/abicheck-clang-plugin/README.md` (in the `abicheck/abicheck`
repo) and copy it to that same path before running
`bazel build //tools/abicheck:math_abicheck_inputs`.

## Multi-library aggregate gate (architecture review P1-5)

`strings_lib/` (`strings_lib/BUILD.bazel`) is a second, deliberately
independent `cc_library` + `cc_binary(linkshared = True)` target —
unrelated to `//:math`, so a change to one never touches the other's ABI.
It exists to prove the gate architecture generalizes past a single
target, and to exercise `abicheck aggregate` (the CLI command that turns
several independent per-target reports into one combined verdict)
against a real multi-target report set produced by this repo's own CI,
not a synthetic fixture.

It's its own Bazel package rather than folded into root `BUILD.bazel`
alongside `:math`/`:math_api`: `scripts/check_coverage_contract.py`'s own
`buildfiles(deps(//:math))`-based exemption (see above) answers per
*file*, not per target within a file — if `:strings`'s declaration lived
in root `BUILD.bazel` too, any PR touching only `:strings` would still
(correctly, if uselessly) disqualify `:math`'s own coverage-contract
exemption, since Bazel can't tell "this diff to `BUILD.bazel` only
touched an unrelated target" without literal diff-content parsing. A
separate package sidesteps the whole question: root `BUILD.bazel` simply
never changes for a `:strings`-only PR. Its public header
(`strings_lib/include/abicheck_lab/strings.h`) deliberately avoids
`std::string`/STL containers for an unrelated, empirically-verified
reason: a direct-clang dump of a header taking `std::string` pulls in a
multi-megabyte L5 source-graph closure over libstdc++'s own template
internals (visible even at the default `headers` depth), which would
bloat this committed baseline for no reason connected to what this
fixture actually validates.

Unlike `//:math`, `strings_lib`'s baseline (`abi/strings.abicheck.json`,
collected by `baseline.yml`) and PR-time check (`scan_strings` job in
`abi-scan.yml`) are deliberately scoped down to a plain binary+header
`depth: headers` comparison — no Bazel evidence pack, no `depth: source`
replay. That's on purpose: this library's job is to validate the
*aggregate* plumbing across multiple targets, not to duplicate `:math`'s
own full source-evidence pipeline a second time.

`abi-scan.yml`'s `aggregate` job downloads both `abi-report-math.json`
(a copy of the required `scan` job's own canonical report) and
`abi-report-strings.json` (from `scan_strings`) and runs `abicheck
aggregate` against a **declared, fail-closed expected-target manifest**
(a "Build expected-target manifest" step, not a hand-retyped `--expect`
list): `math` is declared `required: true` whenever `scan` itself judged
this PR ABI-relevant (`needs.scan.outputs.relevant`, the same signal
`scan`'s own "Enforce gate" step below gates on). A `math` report
silently missing on an ABI-relevant PR — an `upload-artifact` hiccup, or
`scan`'s compare step failing before it could write a report — is now a
real `aggregate` coverage-axis failure (exit 1) that fails the job, not a
`--discovered-only` non-signal absorbed into "whatever showed up counts
as complete".

`strings` is deliberately **not** declared in the manifest at all —
`abicheck aggregate`'s coverage axis (what `required` controls) and its
*gate* axis are independent: a declared-but-not-required target's own
report still folds into the overall gate the moment it's discovered, so
`required: false` alone would still let a genuinely BREAKING
`strings_lib` compare fail this job, contradicting `scan_strings`'s own
deliberately best-effort/non-blocking compare step. `--on-unexpected-
target warn` is what actually decouples "surfaced in the job summary"
from "gates this job" for an undeclared target's report.

Only on a PR `scan` didn't judge ABI-relevant does the manifest end up
empty, and the step falls back to `--discovered-only` — nothing was
expected, so nothing to gate, purely informational as before. `:math`'s
own required gate (the `scan` job's "Enforce gate" step) is unaffected
either way; this job additionally validates that the aggregate CLI's own
plumbing produced the report `scan` was actually supposed to produce.

## `cc_shared_library` target shape (`capabilities.yaml`: `cc-shared-library-target-shape`)

Every other target in this repo — `//:math`, `//strings_lib:strings`,
every `fixtures/*` scenario pair — is built as `cc_binary(linkshared =
True)`: a `cc_binary` whose `linkshared` attribute makes Bazel emit a
`.so` instead of an executable. That's a real, common way to produce a
shared library with Bazel, but it isn't the *only* one, and a review of
this lab's own coverage flagged it as a gap: nothing exercised a genuine
`cc_shared_library` target — the rule Bazel actually documents for a
shared library assembled from one or more `cc_library` dependencies, with
its own distinct action graph (a real link action producing the shared
object, not `cc_binary`'s implicit one) and its own export-surface
semantics.

`//:math_shared` (root `BUILD.bazel`) closes that gap, deliberately by
addition rather than by migration: `math_impl` is an ordinary, private
`cc_library` (the same `src/math.cc` `//:math` already builds, just never
linked into a binary on its own), and `math_shared` is a `cc_shared_library`
wrapping it. `//:math` itself is untouched — every existing job's cache
key, evidence pack, and committed baseline (`abi/math.abicheck.json`)
stay keyed to `//:math` exactly as before; migrating the required gate's
own target shape is a separate, much larger decision this PR doesn't
make. Verified directly (not assumed) that the two shapes currently
produce an identical export surface for the same source:
`nm -D bazel-bin/libmath.so` and `nm -D bazel-bin/libmath_shared.so` list
the same three defined symbols (`Calculator::add`, `Calculator::multiply`,
`api_version`) for unchanged `src/math.cc`.

`abi-scan.yml`'s `scan_math_shared` job builds `//:math_shared` and
`compare`s it (headers-depth, `ast-frontend: clang`) against its own
committed baseline, `abi/math_shared.abicheck.json` — collected by
`baseline.yml` the identical, lightweight way `abi/strings.abicheck.json`
is (no Bazel evidence pack, no `depth: source` replay; this target
exists to validate the target-shape mechanism, not to duplicate `:math`'s
own full source-evidence pipeline). Deliberately non-gating, same posture
as `scan_strings`: continue-on-error throughout, no "Enforce gate" step,
never able to block a PR that never touched `//:math_shared`.

Still open, and deliberately not attempted here (see "Known limitations"
below): `scenarios/manifest.yaml`'s own detection-correctness fixture
pairs (`fixtures/*`) are still all `cc_binary(linkshared = True)`-shaped
— proving abicheck's *detectors* behave identically against a
`cc_shared_library`-shaped target, not just that one such target can be
built and scanned, is a separate, still-open axis.

## Consumer/app-scoped validation (architecture review P1-6)

`consumer/` (`consumer/BUILD.bazel`) is a real, separately-built
application binary (`consumer_app`) that dynamically links against
`//:math` (via a `cc_import` wrapping its shared-library output) and
calls only a subset of its public API — `Calculator::add()`, deliberately
never `Calculator::multiply()` or `api_version()` (see `consumer/app.cc`'s
own comment). It exists to exercise abicheck's `compare --used-by`
app-scoping machinery against a *real* consumer's actual dynamic-symbol
imports, not a hand-written or synthetic import list.

`abicheck compare --used-by <consumer_app>` reads the consumer binary's
own `.dynsym` undefined-symbol table to scope the comparison: a change to
a function this consumer never calls (`::multiply`) is demoted to
informational context (folded into the report's `full_verdict`/
`full_summary`, kept for visibility but not driving the primary verdict),
while a change to one it does call (`::add`) still drives the app-scoped
verdict and exit code. This is the strongest evidence this repo's gate
architecture asks of the analyzed artifacts without ever executing them.
**`--verify-runtime`** — a probe that additionally ran the consumer binary
once against each library side (`LD_BIND_NOW=1`) and recorded a
runtime-load-failure finding if the dynamic linker rejected the new
library — is **removed from upstream abicheck** (it had already been
reduced to a documented, always-no-op safety stub before removal; see
upstream's `--verify-runtime` execution-probe removal and its
`security_trust_boundary_hardening` changelog entries) and is no longer
passed by this repo's workflows or composite-Action invocations.

`abi-scan.yml`'s `consumer_scoped` job runs this comparison on every
ABI-relevant PR, publishing an `abicheck-consumer-scoped` artifact and
job summary. Deliberately non-gating (no "Enforce gate" step, same
posture as `scan_strings`/`aggregate` above): this job validates the
app-scoping *mechanism*, not this fixture's own ABI, and must never
block a PR that never touched `consumer/`.

**Both `consumer_app` and the "old" library are deliberately built from
the PR's *base* SHA (a `git worktree` checkout), never from HEAD.**
`--used-by` exists to answer "would an already-deployed, already-compiled
consumer still work" — rebuilding `consumer_app` from HEAD instead
answers a much weaker question ("does freshly-recompiled source still
compile and link"), which is trivially true even for a source-compatible-
but-ABI-breaking change (e.g. a parameter type change the existing call
site still compiles against unchanged): a HEAD-rebuilt consumer imports
the *new* mangled symbol and would report full coverage even though the
real, already-shipped binary imports the *old* symbol and would fail to
load. Building the historical `consumer_app` from base SHA answers the
real question, and the historical `libmath.so` is used as `old-library`
for the same run so both sides of the comparison come from one
consistent, already-shipped build. The PR that first introduces
`consumer/` has no historical copy to build against at its own base SHA
— that bootstrap case degrades to a HEAD-rebuilt fallback with an
explicit caveat in the job summary, not a hard failure; every PR after
this one merges gets the real historical-binary comparison.

## GCC/Clang × C++ standard profile matrix (architecture review P1-7)

`//:math`'s real ABI must not depend on which compiler or C++ standard
built it — the same source, the same target triple, the same x86-64
System V ABI. `.bazelrc` deliberately pins the whole workspace to one
fixed toolchain (default gcc, `-std=c++17`) for reproducibility, which is
correct for the required gate but means nothing in this repo's CI ever
proves the gate's own *findings* are actually toolchain-invariant — only
that one fixed toolchain's output compares cleanly against itself.

`abi-scan.yml`'s `toolchain_matrix` job (a GitHub Actions matrix,
`fail-fast: false`) rebuilds `//:math` under four (compiler × standard)
combinations — `{gcc, clang-18} × {c++17, c++20}`, each overriding
`--cxxopt=-std=<standard>` and `CC`/`CXX` on the command line — and runs
`abicheck compare` against the SAME committed baseline every other leg
uses. Any per-leg divergence (a finding that appears under one
compiler/standard combination and not another, despite no real source
change) is directly visible in each leg's own artifact/job summary
instead of silently invisible behind the single pinned toolchain the
required gate always uses. Verified locally: a `gcc -std=c++20` build
already surfaces a real, informative divergence — the compiler-generated
default constructor's export shape differs from the `-std=c++17`
baseline it's compared against, exactly the class of standard-driven ABI
question this matrix exists to surface (not itself an error in the
fixture or the workflow). Deliberately non-gating (no "Enforce gate"
step): this validates toolchain-invariance as a property to *watch*, not
a pass/fail contract this fixture's own ABI must additionally satisfy.

## Cold/warm performance benchmarks (architecture review P1-8)

`performance.yml` — a dedicated, `workflow_dispatch` + weekly-scheduled
workflow (never on every PR: a genuinely cold Bazel build plus a cold
`abicheck dump` is real wall-clock cost, not worth paying per-PR for a
number nobody's blocked on) — measures the actual speedup Bazel's disk
cache and abicheck's own snapshot cache (`XDG_CACHE_HOME`-scoped) give,
rather than assuming one.

- **Bazel**: `bazel clean --expunge` + an empty disk-cache directory,
  then `bazel build //:math`, timed — "cold". `bazel clean` (server/output
  tree wiped, but the disk-cache directory from the cold run kept) and
  the identical build again, timed — "warm", isolating the disk cache's
  own contribution from in-server incrementality (which the cold run
  never had a chance to use either).
- **abicheck**: the identical isolation, one layer up — an empty, dedicated
  `XDG_CACHE_HOME` for `abicheck dump bazel-bin/libmath.so ...`, timed
  cold, then the same command again against the same (now-populated)
  cache directory, timed warm.

Publishes a Markdown cold/warm/speedup table (`scripts/
render_performance_summary.py`) to the job summary and an
`abicheck-lab-performance-timings` JSON artifact (90-day retention, long
enough to eyeball a trend by hand across runs — no trend-reporting
database exists yet, see AGENTS.md's (abicheck/abicheck) own "Deferred
entirely" entry for that same open gap upstream). `contents: read` only,
no PR interaction, never gates.

## Known limitations / follow-ups

This lab currently validates one `cc_library` + `cc_binary(linkshared =
True)` target (`//:math`) with the full `depth: source` gate pipeline,
a second, independent library (`//strings_lib:strings`, see
"Multi-library aggregate gate" above) exercising the `abicheck aggregate`
CLI at a deliberately scoped-down `depth: headers`, a real consumer
application (`//consumer:consumer_app`, see "Consumer/app-scoped
validation" above) exercising `compare --used-by`, and — since the
`cc-shared-library-target-shape` capability closed — a genuine `cc_shared_library`
target (`//:math_shared`, root `BUILD.bazel`) wrapping a private
`cc_library`, coexisting with `//:math`'s own `cc_binary(linkshared =
True)` shape rather than replacing it, scanned (headers-depth,
non-gating) by `abi-scan.yml`'s `scan_math_shared` job against its own
committed baseline (`abi/math_shared.abicheck.json`, refreshed by
`baseline.yml` the same way `abi/strings.abicheck.json` is). The known axes it does
not yet cover are generated from `capabilities.yaml` below (see
"Capability matrix"), not hand-typed here, so the two cannot disagree:

<!-- capability-matrix:gaps:start -->
_No `gap`/`planned` entries are currently declared in `capabilities.yaml`._
<!-- capability-matrix:gaps:end -->

This lab DOES
now have a machine-readable scenario matrix
with an expected/actual oracle per patch (fixtures/patches/scripts that
apply a change to a clean fixture, run a scan, and assert on the JSON —
rather than relying on long-lived PRs and human eyeballing of comments) --
see "Scenario validation" below -- and a **generated-header** scenario
closing that specific gap: `generated_header_removed_function`
(`fixtures/generated_header/`) sources its public header from a Bazel
`genrule` rather than a checked-in file, proving abicheck's header
ingestion works against a build-output header, not just a source-tree one.
Getting this working surfaced a real GCC quote-include mechanics gotcha,
worth recording since it'll bite any future generated-header fixture the
same way: a bare `#include "lib.h"` only ever resolves via gcc's own
same-directory-as-the-compiling-file check, which can only succeed for a
real, checked-in source-tree file -- for a header that's a build output,
that check fails and `-iquote bazel-out/.../bin` (Bazel's own fallback,
which appends the include text verbatim to the bin root, not the
compiling file's own package path) never finds it either unless the
`#include` spells out the full package-relative path
(`#include "fixtures/generated_header/v1/lib.h"`, not bare `"lib.h"`) --
confirmed with `CcInfo.compilation_context.headers` correctly listing the
generated file all along, so this is a compiler include-search-order
detail, not a Bazel dependency-graph bug.
A security scenario specifically demonstrating "breaking code change +
modified committed baseline in the same PR still gates red" also hasn't
been exercised as an actual test PR yet — the architecture above (baseline
read from base SHA, `abi/**` in `CODEOWNERS`) should already guarantee it
structurally, but that's a claim, not yet a verified test. These are
tracked as follow-up work rather than folded into this change.

## Capability matrix

The "does not yet cover" claims two paragraphs above (generated, not
hand-typed — see `scripts/gen_capability_gaps.py`'s marker comments there)
— and every axis this lab *does* cover (evidence depth, header frontend, toolchain,
comparison scope) — used to live only as hand-typed prose here, with
nothing to stop it drifting from the real CI jobs as those were added,
renamed, or removed. `capabilities.yaml`
(repo root) is now the declarative, machine-checked source of truth for
that question: one entry per (axis combination) → (job that exercises it,
or `status: gap` if none does), validated against the real
`.github/workflows/*.yml` job names by `scripts/check_capability_matrix.py`
(wired into `capability-matrix.yml`, path-triggered on either file
changing) — the same "declarative table + one script that checks reality
against it" pattern `scenarios/manifest.yaml` already uses for
fixture-level detection correctness, generalized one level up to CI
*coverage* itself. Two failure modes it catches that pure prose never
could: a matrix entry pointing at a job that no longer exists, and a real
job in `abi-scan.yml`/`scenarios.yml`/`scenarios-canary.yml` that no entry
documents.

Deliberately staged, not built all at once. Phase 1 (this file + its
validator), phase 2 (generating the "Known limitations" gap list above
from this file — `scripts/gen_capability_gaps.py`, checked in CI the same
way, so the two literally cannot disagree), and phase 4 (each
`toolchain_matrix` entry's `matrix_leg` — its real `cc`/`cxx`/`std` —
checked against `abi-scan.yml`'s actual `strategy.matrix.include` by
`scripts/check_toolchain_matrix_sync.py`, so a repointed compiler can't
drift from what this file claims either) are done. Deliberately a
consistency *check* rather than a live GitHub Actions dynamic matrix (a
prior job emitting JSON consumed via `fromJson()`): that would restructure
a real, already-working job in the workflow this repo protects most
carefully, with no way to dry-run the result outside an actual Actions
run — this gets the same "can't silently drift" property without that
risk. Phase 3 (a shared reusable-workflow leg replacing the boilerplate
`abi-scan.yml`'s jobs currently duplicate) is partially done: the
checkout + "Skip if nothing ABI-relevant changed" pair, and the
"Resolve trusted baseline from PR base SHA" step, are now
`.github/actions/skip-check` and `.github/actions/resolve-baseline` —
two composite actions, each called from all 5 jobs that need it
(`scan`, `scan_strings`, `consumer_scoped`, `toolchain_matrix`,
`l4_clang_plugin`) instead of five duplicated copies. Deliberately does
NOT extract the "Set up Bazel disk cache" step that follows —
see `.github/actions/skip-check/action.yml`'s own docstring for why: its
cache-key `hashFiles()` input list genuinely differs per job (root
`BUILD.bazel` alone for `scan`/`toolchain_matrix`, plus
`strings_lib/BUILD.bazel` for `scan_strings`, plus `consumer/BUILD.bazel`
for `consumer_scoped`, plus two `tools/abicheck/*` files for
`l4_clang_plugin`), so folding it in would trade real duplication for a
wide, easy-to-misuse parameter surface instead of a clean extraction.
Verified structurally rather than via a live Actions run (not possible
here): every step from "Set up Bazel disk cache" onward, in every one of
the 5 touched jobs, parses as byte-for-byte identical before and after
this extraction (`yaml.safe_load`-level diff), and every one of the ~30
existing `steps.relevance.outputs.relevant`/10
`steps.resolve_baseline.outcome` references elsewhere in those jobs was
left completely untouched — the invoking step in each job keeps the same
`id: relevance`/`id: resolve_baseline` it always had, since a composite
action's outputs (and `continue-on-error`) are exposed on the step that
calls it, not on any step inside the action itself.

### Capability receipts (phase 5)

Phases 1–4 above prove `capabilities.yaml` stays honest about *what CI is
wired to run* — every `covered`/`non_gating_watch` entry names a real
`job` in a real workflow. They cannot prove the thing that actually
matters for trusting a `gating: true` entry: that the job *ran this
workflow run* and actually reached a passing result, rather than being
silently skipped, degraded, or left dangling behind an unrelated failure
earlier in the same job. A matrix entry pointing at a job that exists says
nothing about whether that job's gating step executed to completion on
the run a required check is being judged against.

`scripts/capability_receipts.py` closes that gap with a small,
machine-written JSON fact per `gating: true` capability id
(`status: passed | failed | skipped`, plus which workflow/job/run/sha
produced it), written immediately after the step(s) that capability's
entry describes and validated before anything downstream trusts it:

- `scan`/`aggregate` in `abi-scan.yml` each emit their own receipt
  (`scripts/emit_capability_receipt.py`) for `math-source-gate`/
  `aggregate-multi-library` right after their own gating logic runs, in
  every case — `status: skipped` (not a missing receipt) when a PR was
  judged not ABI-relevant, so a validator can tell "genuinely nothing to
  prove this run" apart from "the job silently produced nothing".
- A new `verify_capability_receipts` job (`needs: [scan, aggregate]`,
  `if: always()`) downloads both receipts and fails closed via
  `scripts/validate_capability_receipts.py` if either is missing or not
  `status: passed`.
- `scenarios.yml`'s own `scenarios` job derives
  `detection-correctness-scenarios-{castxml,clang}`'s receipts from
  `run_scenario.py`'s existing `summary.json`
  (`scripts/emit_scenario_receipts.py` — one receipt per header-frontend
  profile: `passed` only if every result for that profile passed, `failed`
  on any mismatch, `skipped` if the manifest currently declares no
  scenario for that profile at all) and validates them in the same
  "Enforce gate" step that already gates on `run_scenario.py`'s own exit
  code.

`status: skipped` is accepted only for a capability id explicitly passed
via `--allow-skip` — never by default. `abi-scan.yml`'s own validation
step passes `--allow-skip math-source-gate --allow-skip
aggregate-multi-library`: `math-source-gate` skips (with a stated reason)
when `skip-check` judges the PR not ABI-relevant, and
`aggregate-multi-library` skips only in the identical tolerated case its
own job already accepts (nothing declared, and the best-effort
`--discovered-only` fallback didn't succeed) — mirroring, not
loosening, what `abi-scan.yml`'s existing gates already treat as clean.
`scenarios.yml`'s own validation step passes no `--allow-skip` at all:
`detection-correctness-scenarios-{castxml,clang}` have no legitimate
"not applicable this run" condition (the `scenarios` job always runs
every declared scenario), so a `skipped` receipt there — every scenario
for that profile silently removed from `scenarios/manifest.yaml` — fails
loudly instead of passing vacuously. `status: failed` always fails
validation regardless of `--allow-skip`.

A schema-valid, `status: passed` receipt still proves nothing on its own
if it wasn't actually produced by the job capabilities.yaml itself
declares for that capability id, on the run being judged — so the
validator also cross-checks a receipt's own `workflow`/`job` against
capabilities.yaml's own declaration for that id, requires `run_id`/`sha`
to be populated at all, and (via `--expect-run-id`/`--expect-sha`, which
both workflows' own validation steps pass as `${{ github.run_id }}`/
`${{ github.sha }}`) rejects a receipt left over from a different run.

One residual gap `--allow-skip` doesn't close: `verify_capability_receipts`
(and `scenarios.yml`'s own "Enforce gate" step, already required via its
own job) is a *separate* job/step from `scan`/`aggregate`, so its own
failure only blocks a merge once it's added to this repository's required
status checks in branch protection — the same "only has teeth once branch
protection requires it" caveat `.github/CODEOWNERS` already states for
what it protects.

Deliberately scoped to today's 4 `gating: true` entries only, and
deliberately minimal (status + provenance, no scanner ref, no
effective-config digest, no semantic assertions on the underlying
report's own findings) — a natural next phase for a fuller downstream
conformance platform, not attempted here. `tests/` (run by
`capability-matrix.yml`'s own `pytest tests/` step) covers the schema,
both emitters' status derivation, and the validator's failure modes
(`MISSING_RECEIPT`, `FAILED`, `UNEXPECTED_SKIP`, `MALFORMED_RECEIPT`, an
id filtered out of scope, an `--allow-skip` id outside that scope, and
that an allowed skip is accepted) directly, without needing Bazel/castxml
to exercise them.

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
| `generated_header_removed_function` | v2's Bazel-*generated* header drops an exported function | `BREAKING` |

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
silently dropped): a `cc_shared_library` *detection-correctness scenario*
(fixtures/`scenarios/manifest.yaml`'s own `v1`/`v2` compare pairs are
still all `cc_binary(linkshared = True)`-shaped, unlike root
`BUILD.bazel`'s standalone `//:math_shared` target above, which the
capability matrix now covers). `fixtures/`/`scenarios/` themselves are still single-library
`v1`/`v2` compare fixtures — the multi-library *aggregate-plumbing* gap
is covered separately by `strings_lib/`'s own CI wiring (see
"Multi-library aggregate gate" above) and the consumer/app-scoped gap by
`consumer/`'s own CI wiring (see "Consumer/app-scoped validation" above),
neither by a `scenarios.yml` entry. These are real follow-up work, not
abandoned scope.
