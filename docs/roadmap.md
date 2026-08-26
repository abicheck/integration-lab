# Roadmap

This describes planned phases for the integration lab, in the order they
were conceived — some are already done (marked **done** inline, with what
shipped and what's still open), most are not. **No dates, and no claim
that the phases *not* marked done are anywhere close** — this is a
statement of direction, not a committed schedule. See
[integration-profiles.md](integration-profiles.md) for the authoritative
account of what is implemented today.

1. **Run the real ABICheck scanner for the CMake and Make profiles: done.**
   `ci/real_scan.py` runs a real `abicheck dump --depth source`/`compare`
   invocation for those two profiles (see
   [integration-profiles.md](integration-profiles.md#real-scanner-migration-for-cmakemake-done-roadmapmd-item-1)),
   the way the Bazel profile is already scanned by `abi-scan.yml`. CMake's
   `compile_commands.json` and Make's `bear`-generated equivalent both feed
   `--build-info` directly, filtered to each target's own translation unit.
   `ci/check_profile.py`'s existing ELF/header mechanism remains in place
   only for the Bazel profile's own leg of this same advisory workflow —
   Bazel already has its own real, required gate in `abi-scan.yml`, so it
   never needed a second real-scanner path here. What's still open: this
   is still `integration-shadow.yml`'s advisory workflow, not a required
   gate (see item 4 below). Make evidence is now fail-closed: Bear and a
   non-empty compile database are mandatory for its contract profile.
2. **Consume native `check-project`: done at binary depth.**
   `.github/workflows/project-shadow.yml` validates `.abicheck.yml` and every
   standardized build output, restores exact-base accepted-main baselines,
   and invokes upstream `check-project.yml`. Its run-plan oracle derives the
   exact target/profile/channel/depth cells from the project declaration and
   verifies each cell's required/gate policy, rejecting duplicate `check_id`
   values and malformed cells before either can join the comparison set.
   Baseline resolution is receipted (see
   [Promoting the native project aggregate](#promoting-the-native-project-aggregate)).
   Source-depth promotion remains blocked on upstream target-specific
   build-output evidence projection and is recorded as an `expected_gap`, not
   emulated with lab routing glue.
3. **Broaden cross-build-system public-contract equivalence.** An advisory
   version already exists (`ci/compare_build_outputs.py`, the
   `cross_build_equivalence` job — see
   [integration-profiles.md](integration-profiles.md#cross-build-system-equivalence)):
   it compares the three profiles' own staged output against *each other*,
   not just each against its own baseline, and already found a real SONAME
   mismatch across all three build systems. Promoting it to required, and
   extending the scenario oracle's own build-system parity
   (`--build-system cmake` currently covers only `add_function`/
   `remove_function`; Make has none yet) beyond its current initial
   subset, both wait on the build definitions being intentionally aligned
   and stable first.
4. **Promote proven profiles to required contracts.** Once a profile has
   run the real scanner reliably over time, evaluate flipping its
   `contract` field and adding its check to branch-protection required
   status checks — a deliberate, separate decision from simply having the
   profile exist.
5. **Accepted-main and release-contract profile baselines: done for the
   profiles that have one today.** `profile-baseline.yml` keeps every
   profile's own `abi/profiles/<id>/*.abicheck.json` current on every
   push to `main` (the same pattern `baseline.yml` already uses for the
   canonical gate); `release.yml` re-certifies and publishes an immutable
   per-profile baseline asset on every published release, currently only
   for the one `contract: true` profile
   (`linux-x86_64-gcc14-cxx17-bazel`). Extending either beyond that one
   profile is the same promotion decision as item 4 above, not separate
   work.
6. **Scanner release-candidate dispatch: done.** `.github/workflows/canary.yml`
   (see [integration-profiles.md](integration-profiles.md#scanner-candidate-certification-canary))
   certifies a specific `abicheck` commit — via a `repository_dispatch`
   payload, a manual `workflow_dispatch` ref, or the weekly default of
   `abicheck/main` — against the semantic scenario oracle, the real
   composite Action surface, and every multi-build-system profile's own
   staged output, and classifies the result into one of five outcomes
   (`scripts/classify_canary_outcome.py`). What's still open: the
   classification is one signal per job, not per step (a real, documented
   scoping limitation, not a finer breakdown this PR claims), and it never
   runs the real scanner's `--against`-a-committed-baseline path at all
   (see `UPSTREAM_TO_ABICHECK.md`'s own P0 entry for why) -- only
   self-comparisons, so it cannot yet catch a regression that only shows
   up when comparing against a *different*, real baseline.
7. **Expand toolchain and platform profiles based on real integration
   needs.** Additional `(compiler × standard × platform)` combinations, or
   additional build systems, added when a real integration need
   demonstrates the gap — not speculatively ahead of one.

## Promoting the native project aggregate

`project-shadow.yml`'s aggregate job stays advisory until the native path has
survived real traffic. `contract: true` in `.abicheck.yml` marks a profile's
*declared* status; it is not evidence, and the repository branch itself is
still unprotected with no required status checks. Promotion to a required
status check needs all seven of the following, each on an ordinary PR with no
bootstrap or baseline-refresh special case involved:

1. At least one clean ordinary PR — every cell green, aggregate green.
2. One binary-breaking PR — caught at `depth: binary`, aggregate red.
3. One L4-only API-breaking PR — invisible at binary depth, caught at source
   depth, aggregate red. (Blocked on source-depth cells; see item 2.)
4. One missing-baseline or missing-report PR — the fail-closed path goes red
   rather than green-by-absence, with a baseline-resolution receipt naming
   the identity that did not match.
5. One upstream pin bump — `ci/abicheck-version.yaml`'s `candidate_sha`
   certified by the canary, then promoted to `sha`, with every workflow ref
   moving in the same reviewed commit (`tests/test_abicheck_pin.py`).
6. No run in the set may have taken the bootstrap path
   (`native_ready == false`) or have been decided by a baseline-refresh
   special case.
7. Aggregate status matches every cell — no cell red with the aggregate
   green, and no aggregate red without a red cell.

Until all seven have happened on real PRs, the Bazel gate in `abi-scan.yml`
remains the one branch-required compatibility check.

## The scanner pin

There is one reviewed ABICheck pin, `ci/abicheck-version.yaml`. Every install
and every reusable-workflow reference derives from it; where GitHub syntax
forbids interpolating a `uses:` ref, `tests/test_abicheck_pin.py` fails on any
divergence instead. Bumping it follows one order:

```text
candidate upstream SHA
    -> canary certification
    -> integration-lab PR pin bump
    -> all scenarios + real accepted-main baseline comparison
    -> reviewed merge
```

The legacy single-target scan/scenario suite still runs on a second pin
(`legacy_sha`), reviewed in the same file, and is migrating onto `sha` one
workflow at a time.

## Declared follow-ups

These are named from the README rather than left as prose, so what is not
yet done is as legible as what is.

**Item 10 — compare the actual wheel: done, except post-repair.**
`depth-scenarios.yml`'s `python-wheel` job now builds both wheels and runs
`abicheck compare old.whl new.whl`, asserting extension discovery inside
the package, `.pyi` discovery inside the package, the Python API findings,
bundled native library discovery, package-level added/removed extensions,
and wheel tags/CPython ABI metadata (including that an extension wheel is
not `Root-Is-Purelib`). Expectations are declared by module stem so the
scenario is not pinned to one interpreter. The standalone `.so` scenario
stays as the fast unit-level acceptance case and is described as one.
What remains: post-repair wheel behaviour where auditwheel/delocate/
delvewheel applies — the repaired wheel's vendored library directory,
its rewritten RPATHs, and the dependency closure those imply.

**Item 12 — build-system scenario parity: done for the shared-shape
suite.** `scripts/run_scenario.py` gained a `--build-system make` path
(`buildsystems/make/fixtures/Makefile`, the Make counterpart of the generic
CMake fixture project), and `scenarios/build-matrix.yaml` now declares
every scenario whose fixture has the shared `lib.cc` + `lib.h` shape for
both CMake and Make.

The bar is not "it builds": `scripts/check_scenario_parity.py` compares the
reports ACROSS build systems and fails unless the same semantic mutation
yields the same normalized findings, severities, suppressed symbols and
verdict everywhere it ran. Checking each run against its own oracle
separately would pass even if the three disagreed with each other — and on
its first run this check found exactly that, a `soname_bump_recommended`
finding CMake produced and Make did not, because a bare `g++ -shared` sets
no SONAME while CMake and Bazel both do. The Make recipe now sets it.

What remains: `generated_header_removed_function`, whose header is produced
by its own Bazel genrule rather than committed to the fixture directory, so
there is nothing for a `FIXTURE_DIR` recipe to compile. Factoring the
generation step out of the Bazel rule would bring it in. The
cross-DSO/SONAME/private-header/`_GLIBCXX_USE_CXX11_ABI` mutations named in
the original list need new fixtures of the same shared shape before they
can join the matrix; they are covered elsewhere (cross-DSO by
`project-cross-dso`, SONAME by the loader-feature suite) but not yet as
three-build-system parity cases.

**Item 9 — plugin-pack reuse in a clean job.** The plugin scenario should
run three distinct stages: build with the plugin; verify and upload the
facts pack; then a *new clean job* that downloads binary, headers and pack
and performs a source-depth compare with no source checkout and no build.
Required assertions: the same normalized finding set as replay, the same
effective depth, the same target accounting, no hidden replay fallback, a
stale source-tree digest rejected, a missing TU rejected, a wrong plugin
LLVM major rejected, empty public roots rejected, and a corrupted pack
reported as an operational error.

**Item 14 — generated demonstration PRs.** The long-lived test PRs have
drifted: the default-argument PR still describes adding a default argument
as a source/API break, and PRs #12 and #14 pin obsolete scanner revisions,
so their reports are dominated by old evidence-comparability problems
rather than the scenario each advertises. Replace the hand-maintained
branches with generated ones — reset to latest main, apply the scenario
patch, force-push, regenerate the expected-result section of the PR body —
each carrying a natural abicheck result (possibly red) plus a separate
scenario-oracle check that is green only when the natural result is exactly
the expected one.

**Items 4 and 5 — target-specific evidence and source-depth cells.** Each
target entry in `build-output.json` should declare its own evidence pack:

```yaml
evidence:
  kind: source-facts
  path: evidence/<target>/abicheck_inputs
  projection: declared
```

with one validated pack per target (`evidence/core/abicheck_inputs`,
`evidence/math/...`, `evidence/strings/...`) and no two targets claiming
the same `projection: declared` pack. Only once that works can the project
topology carry real source-depth cells — `math` at `depth: headers` and
`depth: source`, required and deferred — and the L2/L4 scenario validate
the same native project pipeline rather than a standalone script. Both are
sequenced with the upstream pin bump (see [The scanner pin](#the-scanner-pin)):
they depend on upstream target-specific evidence forwarding, which landed
after the currently pinned SHA, and are recorded as
`target-specific-source-evidence-routing` in `scenarios/manifest.yaml`'s
expected gaps until then.
