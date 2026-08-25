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
   verifies each cell's required/gate policy. Source-depth promotion remains
   blocked on upstream target-specific build-output evidence projection and
   is recorded as an `expected_gap`, not emulated with lab routing glue.
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
