# Migration map: integration-lab as a thin external acceptance environment

This note is the Step-1/Step-8 deliverable for the "rework integration-lab
into a thin, realistic external acceptance environment" pass. It records
what was actually found (verified against the live repos, not assumed from
an old assessment), what was implemented in this pass, and what stays
blocked and why. It is a snapshot, not a new parallel status doc: the
detailed, load-bearing accounts stay in `docs/roadmap.md`,
`docs/scenarios-and-capabilities.md`, `docs/integration-profiles.md`, and
`UPSTREAM_TO_ABICHECK.md` — this note points at those rather than
duplicating them, and only restates what's needed to explain this pass's
own findings and choices.

## 1. Current truth, verified against the reference point in the brief

The brief's reference point (integration-lab `b50d357`, upstream
`1a59fdb0`) described three competing integration paths, an old native
project pin, binary-depth-only native cells, and a non-contract Clang
profile with no provenance capture. Checked directly against this repo's
current `main` (`da621e8`, one commit ahead of the branch point):

- **Competing integration paths: down to one product-facing path, plus one
  explicitly-labeled migration lane.** `.abicheck.yml` + `project-shadow.yml`
  + `project-baseline.yml` is the one native `check-project` path (pinned to
  `ci/abicheck-version.yaml`'s `sha`, `42da2d2f9...`). The older
  single-target `scan`/`compare` suite (`abi-scan.yml`, `baseline.yml`,
  `scenarios.yml`, pinned to the same file's `legacy_sha`,
  `b299afdc2277...`) still exists and is explicitly documented as a
  migration lane in `docs/roadmap.md`'s "The scanner pin" section
  ("migrating onto `sha` one workflow at a time"), not silently duplicated
  product logic. `integration-shadow.yml`'s multi-build-system profile
  matrix (`ci/profiles.yaml`) is a third, but it is explicitly advisory —
  `abi-scan.yml`'s Bazel gate remains the one required check
  (`docs/roadmap.md`'s "Promoting the native project aggregate" names the
  seven concrete criteria still unmet). This is a real reduction from "three
  competing paths" to "one product path + one labeled legacy lane + one
  labeled advisory shadow matrix", not three peers.
- **Native project pin:** `ci/abicheck-version.yaml`'s `sha` is
  `42da2d2f947d9eaa42e7d5f334bd2098bb5f08e7`, current as of this repo's own
  `main`; `legacy_sha` was bumped 2026-09-07 (commit `011a80e`, this
  branch's own parent) past a real upstream `scan`/`dump` fingerprint bug
  (`include_sequence` mismatch) documented and reproduced in
  `UPSTREAM_TO_ABICHECK.md`'s "P0" / "RESOLVED" entries. Not stale.
- **Normal native cells: still binary-depth only, and this remains a real,
  explicitly recorded gap**, not fixed since the reference point. Every
  `checks:` entry in `.abicheck.yml` (core/math/strings/consumer-app/
  math-plugin-contract/the `sdk` bundle) declares `depth: binary`. `docs/
  roadmap.md` item 2 and its "Promoting the native project aggregate"
  section record this as blocked on upstream target-specific build-output
  evidence projection ("Items 4 and 5" at the bottom of that file), not
  emulated with lab routing glue. Confirmed unchanged.
- **Clang profile: still `contract: false`, but WITH real provenance
  capture — this is better than the reference point described, not
  unchanged.** `.abicheck.yml`'s `linux-x86_64-clang18-cxx17-cmake-ninja`
  entry is explicit that it "holds the producer-compiler axis and has no
  accepted-main baseline yet" (`contract: false` by design, not oversight).
  `ci/backends/base.py`'s `_tool_version()` (`<executable> --version`,
  captured verbatim, not inferred from the profile id string) is called by
  every backend's `describe()`/`collect_evidence()` and staged into each
  profile's own `provenance/build-system.json` — so "the Clang profile
  actually used Clang" is a real, captured claim (the compiler's own
  `--version` output), not just a profile-name assertion. It is correctly
  still non-`contract` because it has no baseline yet, which is a
  different, honest gap from "not proven to use Clang".

**Conclusion:** three of the four reference-point gaps this brief describes
are already closed or substantially improved on current `main`; the fourth
(source-depth cells) is real, already recorded, and blocked on upstream —
not something this repo can close by itself. No further action on these
four axes was taken in this pass beyond re-verifying them, since re-fixing
already-fixed things would misrepresent this pass's own contribution.

## 2. What this pass implemented (the mandatory real slice)

`docs/roadmap.md` item 12 already closed build-system parity for every
*shared-shape* fixture (checked-in `lib.h`) across Bazel/CMake/Make, with
one documented exception: `generated_header_removed_function`, whose header
is a Bazel genrule *build output*, not a checked-in file — CMake's and
Make's generic, single-recipe fixture drivers had nothing to compile
against, so that scenario stayed Bazel-only, explicitly labeled "no build
mapping declared" (never silently skipped).

This pass closes that exception — a small, coherent, S06-shaped
(build-system parity) vertical slice, fully within the product/ownership
boundaries in Step 2 (no scan-based workflow added, no product semantics
reimplemented, `abicheck compare` stays the only verdict source):

- `fixtures/generated_header/v{1,2}/header_functions.txt` — a new,
  minimal data file (one exported function name per line) declaring the
  same function list the existing Bazel genrule's `cmd` already hardcodes.
- `buildsystems/cmake/fixtures/CMakeLists.txt` and
  `buildsystems/make/fixtures/Makefile` (the existing generic,
  parameterized-by-`FIXTURE_DIR` recipes — no new per-fixture files) now
  run `fixtures/generated_header/gen_header.py` — the *same* generator
  script the Bazel genrule already uses — whenever a fixture ships a
  `header_functions.txt`, writing the header into a scratch directory that
  mirrors the fixture's own repo-relative path. `lib.cc`'s existing
  full-repo-path `#include` then resolves identically under all three
  build systems, so **`lib.cc` and `BUILD.bazel` needed zero changes** —
  no duplicate implementation, no lab-side product logic, purely a build
  recipe extension.
- `scripts/run_scenario.py` gained `_resolve_fixture_new_header()`: for
  the CMake/Make paths, it uses the manifest's declared checked-in header
  when that path actually exists (every other scenario, unchanged
  behavior), and falls back to whatever the build just generated only when
  it doesn't (this one scenario). The Bazel path is untouched.
- `scenarios/build-matrix.yaml` gained the `generated_header_removed_function`
  entry (aliased to both `cmake` and `make` via the existing `&id001`
  anchor).
- `tests/test_generated_header_parity.py` (new) cross-checks the Bazel
  genrule's own `cmd` function list against `header_functions.txt` so the
  two declarations — real duplication, since Bazel's `cmd` is a shell
  string, not a `data` dependency on the `.txt` file — cannot silently
  drift apart.
- `tests/test_run_scenario.py` gained coverage for the new resolution
  function (declared-path-exists, checked-in-fallback, generated-fallback,
  fail-closed-when-nothing-exists) and for `run_one()`'s own plumbing of
  the resolved header through to `run_one_profile`.
- `README.md`, `docs/roadmap.md` (item 12), `docs/scenarios-and-
  capabilities.md` updated to state the closed gap accurately — the prior
  text ("Make has none yet", "stays Bazel-only") was stale relative to
  `main`'s actual `build-matrix.yaml` (Make was already fully aliased to
  CMake) and this pass's own change; left uncorrected, it would have been
  exactly the kind of "old assessment presented as current state" this
  brief's Step 1 asks to avoid.

No workflow YAML changes were needed: `.github/workflows/integration-
shadow.yml`'s `scenarios_cmake`/`scenarios_make`/`scenario_parity` jobs
already run the full suite generically (`run_scenario.py --build-system
{cmake,make}` with no scenario allow-list), so the new scenario is picked
up automatically.

### Local verification (exact commands and results)

Environment: this sandbox has no `bazel`/`bazelisk` and no `gcc-14`/`g++-14`
preinstalled (only system `gcc`/`g++` 13.3.0, `cmake`, `make`); `gcc-14`/
`g++-14` were symlinked to the system compiler *for this local check only*,
not committed anywhere. `abicheck` (the exact pinned `legacy_sha`,
`b299afdc2277a3c9857c413058177c8f6472fcd0`) and a real CastXML were
installed with real, working outbound network access in this session.

```console
python3 -m pytest tests/ -q
  -> 813 passed, 2 skipped   (full existing suite, no regressions)

python3 -m pytest tests/test_run_scenario.py tests/test_generated_header_parity.py -q
  -> 21 passed

python3 scripts/run_scenario.py --only generated_header_removed_function --build-system cmake
python3 scripts/run_scenario.py --only generated_header_removed_function --build-system make
  -> PASS [clang]: expected=BREAKING actual=BREAKING   (both build systems)
  -> FAIL [castxml]: report unreadable — see below

python3 scripts/run_scenario.py --build-system cmake   (full suite, all 7 scenarios)
python3 scripts/run_scenario.py --build-system make    (full suite, all 7 scenarios)
  -> every [clang] profile PASSED for every scenario, including the new one
  -> every [castxml] profile failed IDENTICALLY across every scenario
     (not specific to the new one) -- see below

python3 scripts/check_scenario_parity.py --results cmake=<dir> make=<dir> --allow-partial
  -> zero MISMATCH lines; only "coverage shrank" (missing castxml reports,
     uniformly, same reason as above)
```

**One real, environment-only limitation this pass could not fully close
locally:** the sandbox's `apt install castxml` provides CastXML 0.6.3;
`abicheck` requires `>=0.6.11,<0.8.0` and refuses to run below that floor
(a real, correct guard — verified by reading the actual error, not
assumed). This affected **every** scenario's `castxml` profile equally,
old and new alike, so it is not evidence of a bug in this pass's own
change — the `clang` profile (which does not depend on CastXML) passed for
every scenario under both CMake and Make, including the new one, proving
the header-generation-and-inclusion mechanism itself works end to end with
a real compiler and a real `abicheck compare`. CI installs abicheck's own
pinned CastXML build (`action/install-castxml.sh`, already wired into
`scenarios_cmake`/`scenarios_make`/`scenario_parity`) and is expected to
pass the `castxml` profile too; this was not independently re-verified by
triggering a real CI run in this pass (see §4).

## 3. Scenario portfolio (S01–S10): status, not reimplementation

Per Step 5, this pass does not implement the full S01–S10 portfolio. What
this repo's *existing* CI already covers, mapped to the portfolio's own
labels (verified against real code/tests, not the portfolio prose alone):

- **S01** (visible compatible evolution + private-only control) and
  **S03**'s simplest case (component-scoped compare): covered by the
  existing `abi-scan.yml` Bazel gate plus `project-shadow.yml`'s native
  `check-project` path — both pre-date this pass and were re-verified as
  still real (`.abicheck.yml`'s per-target `checks:`, real baselines under
  `abi/`).
- **S06** (producer/build-system parity): this pass's own slice closes the
  one remaining named gap in the existing three-build-system scenario
  matrix.
- **S02** (facts vs. policy acceptance, bounded acknowledgment): **blocked**
  — `docs/roadmap.md`/`UPSTREAM_TO_ABICHECK.md` record no upstream
  bounded-acknowledgment/version-policy primitive; this repo's own
  suppression fixtures (`remove_function_suppressed`, `suppression_partial`)
  already distinguish an exact-symbol suppression from a baseline
  refresh, which is as far as this axis goes today.
- **S04** (evidence enrichment, stripped/missing-DWARF robustness),
  **S05** (plugin collection+reuse — actually substantially covered
  already by `run_plugin_pack_reuse.py`/the `l4_clang_plugin` +
  `plugin_pack_reuse` jobs, see `docs/roadmap.md`'s "Item 9"), **S07**
  (runtime/dependency evolution, deployment floor — partially covered by
  `ci/run_runtime_floor_scenario.py`), **S08** (consumer impact — partially
  covered by the historical `consumer_app` scoped check), **S09**
  (header-only + Python artifacts — partially covered by `bindings/python`
  + the wheel scenario, `docs/roadmap.md` item 10), **S10** (history/
  first-release): each has *some* existing coverage already landed by
  prior sessions (see `docs/roadmap.md`'s own itemized list); none was
  extended in this pass. Recorded here as "not touched this pass", not
  "unimplemented" — conflating the two would misstate this repo's actual
  coverage.

## 4. Upstream handoff — reproducible product gaps (real, not vague)

These are pre-existing, already-filed-in-doc gaps this pass re-verified
still hold on current `main`, kept here as the compact handoff list Step 8
asks for (full detail lives in `UPSTREAM_TO_ABICHECK.md`, not duplicated):

1. **No target-specific build-output evidence routing** — blocks promoting
   native project cells beyond `depth: binary`. Repro: any `.abicheck.yml`
   `checks:` entry that tries `depth: source` today has no per-target
   `evidence: {kind: source-facts, ...}` pack to point at (see
   `UPSTREAM_TO_ABICHECK.md`'s "Items 4 and 5"). Owner: upstream
   `abicheck` (`service_input_resolution.py`/`buildsource` fold). Status:
   unfiled as a numbered upstream issue in this pass (no issue tracker
   access exercised); tracked here and in `docs/roadmap.md` instead.
2. **`abicheck dump`'s CLI path never builds a `DumpRequest`**, so L3→L2
   build-context folding (`_seeded_compile_context`) never reaches a
   `dump`-produced baseline even though `compare`'s implicit dump does use
   it. Repro and upstream-side acknowledgment already documented in
   `UPSTREAM_TO_ABICHECK.md`'s "P0.3" entry, including the exact upstream
   `AGENTS.md` "Known gaps" citation. Re-verified present as of this pass
   (not re-run against upstream `HEAD` independently).
3. **CastXML version floor (`>=0.6.11`) vs. common package-manager
   availability** — not a bug, but a real, reproducible friction point this
   pass hit directly: `apt install castxml` on ubuntu-24.04 installs 0.6.3,
   which `abicheck` correctly refuses. `action/install-castxml.sh`
   (already used by this repo's own CI) is the correct fix; worth upstream
   documenting more prominently for anyone reproducing scenarios locally
   outside this repo's own CI, since the error message is otherwise the
   only place this is discoverable. Closure test: `castxml --version`
   reporting `>=0.6.11` in a fresh environment before running any
   scenario.

## 5. Deletion candidates — NOT deleted this pass, and why

Per Step 7, nothing was deleted speculatively. Candidates already named
elsewhere in this repo, restated here for visibility:

- `ci/check_profile.py`'s nm/readelf mechanism (the CMake/Make advisory
  signal) — superseded by `ci/real_scan.py`'s real `abicheck dump`/
  `compare` invocation for those two profiles, but kept in place because
  it is still the Bazel profile's own leg of the same advisory workflow
  (`docs/roadmap.md` item 1's own note: Bazel never needed the real-scanner
  path here, since `abi-scan.yml` already gates it directly). Not
  redundant for Bazel; only the CMake/Make legs' old mechanism was already
  replaced (in a prior pass, before this one).
- The legacy single-target `scan`/`compare` suite (`abi-scan.yml`,
  `baseline.yml`, `scenarios.yml`, `legacy_sha`) — superseded in *intent*
  by the native project path, but `docs/roadmap.md`'s own "Promoting the
  native project aggregate" section names seven concrete, unmet promotion
  criteria; deleting the legacy suite before those are met would remove
  the one currently-required compatibility gate this repo has. Not
  touched this pass.
- The five `test/*` demo PR branches (`abicheck/integration-lab` PRs #1–#4,
  #8) — `docs/roadmap.md` item 14 records the regeneration tooling
  (`scripts/gen_demo_prs.py`) as done, with the actual force-push described
  as "an operator step" requiring a deliberate `--push`. This pass did not
  force-push or otherwise touch those branches or PRs — per Step 7,
  rewriting someone else's open PR branches is exactly the kind of act
  that should be asked for by name, not done incidentally inside an
  unrelated rework pass.

## 6. What this pass did NOT do, explicitly

- Did not bump `ci/abicheck-version.yaml`'s `sha`/`legacy_sha` — both were
  already current as of the branch point (`legacy_sha` bumped by this
  branch's own immediate parent commit, `011a80e`, the same day).
  `candidate_sha` (`f7b4fdcc7dafd73ea483b4522a3f63df3a74653f`) remains
  un-promoted, per the pin-bump flow's own requirement of a real canary
  certification run, which this pass did not trigger.
- Did not modify `.abicheck.yml`'s `depth:` fields, `contract:` flags, or
  any baseline-selection/policy semantics — all of that is upstream-owned
  per Step 2 and none of it is safe to fake from the lab side.
- Did not touch any workflow YAML — the new scenario is picked up by the
  existing generic `run_scenario.py --build-system {cmake,make}` calls
  with no per-scenario allow-list, so no workflow change was needed or
  made.
- Did not trigger or observe a real GitHub Actions run of this branch's
  own commits (no push had happened yet at the time this note was
  written — see the top-level session report for whether one was
  triggered afterward, and its run URL/ID if so).
