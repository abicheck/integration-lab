# Roadmap

This describes planned phases for the integration lab, in the order they
were conceived — some are already done (marked **done** inline, with what
shipped and what's still open), most are not. **No dates, and no claim
that the phases *not* marked done are anywhere close** — this is a
statement of direction, not a committed schedule. See
[integration-profiles.md](integration-profiles.md) for the authoritative
account of what is implemented today.

1. **Run the real ABICheck scanner for the CMake and Make profiles.**
   Replace `ci/check_profile.py`'s ELF/header signal with a real `abicheck
   scan`/`compare` invocation for those two profiles, the way the Bazel
   profile is already scanned by `abi-scan.yml`. CMake's
   `compile_commands.json` and Make's `bear`-generated equivalent are both
   candidate `--build-info`-shaped inputs once wired up.
2. **Replace shadow orchestration with `check-target`/`check-project` where
   appropriate.** Once the real scanner runs per profile, evaluate whether
   `ci/run_profile.py`'s current build-then-shadow-check orchestration
   should be replaced by a more direct `abicheck check-target`/
   `check-project`-style invocation per backend, rather than this lab's own
   staging-and-checking scaffolding.
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
