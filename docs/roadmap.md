# Roadmap

This describes planned phases for the integration lab. **No dates, and no
claim of completion** — this is a statement of direction, not a committed
schedule. See [integration-profiles.md](integration-profiles.md) for what
is implemented today.

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
5. **Add accepted-main and release-contract profile baselines.** Extend the
   baseline lifecycle (`baseline.yml`'s pattern) to cover accepted-`main`
   and release-tagged baselines per profile, not just the current
   PR-vs-base-SHA comparison.
6. **Add scanner release-candidate dispatch.** A way to run the full
   profile/scenario suite against an unreleased `abicheck` release
   candidate on demand, ahead of a scheduled canary catching it, to
   de-risk a planned version bump.
7. **Expand toolchain and platform profiles based on real integration
   needs.** Additional `(compiler × standard × platform)` combinations, or
   additional build systems, added when a real integration need
   demonstrates the gap — not speculatively ahead of one.
