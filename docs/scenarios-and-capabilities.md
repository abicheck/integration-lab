# Scenarios, capabilities, and the semantic oracle

This document explains the machine-readable compatibility-scenario system
(`scenarios.yml`, `scenarios-canary.yml`) and the declarative capability
matrix (`capabilities.yaml`). Both validate the real ABICheck scanner
directly — via Bazel, against the root `//:math`-adjacent fixtures — and
are independent of the multi-build-system profiles described in
[integration-profiles.md](integration-profiles.md).

## The semantic scenario oracle

`.github/workflows/abi-scan.yml`'s gate only ever exercises whatever a given
PR's own diff happens to be — it never proves abicheck actually detects a
real break, or correctly leaves a compatible change alone. `scenarios.yml`
closes that gap.

- **Fixtures** — `fixtures/<name>/{v1,v2}/` are small, independent Bazel
  targets, never touching `//:math` itself.
- **Manifest / expected verdicts** — `scenarios/manifest.yaml` is a
  machine-readable **scenario oracle**: a table pairing each fixture with
  its expected `abicheck compare` verdict.
- **Runner** — `scripts/run_scenario.py` builds every `v1`/`v2` pair, runs
  `abicheck compare` between them, and fails if the actual verdict doesn't
  match the manifest. Results are written to
  `scenario-results/summary.json`.

Current scenarios:

| Scenario | Change | Expected verdict |
|---|---|---|
| `add_function` | v2 adds a new exported function | `COMPATIBLE` |
| `remove_function` | v2 removes an exported function | `BREAKING` |
| `change_signature` | v2 changes a parameter type (mangled-name change) | `BREAKING` |
| `generated_header_removed_function` | v2's Bazel-*generated* header drops an exported function | `BREAKING` |

Run locally: `python3 scripts/run_scenario.py` (needs `bazel` and the
`abicheck` CLI on `PATH`, plus `pyyaml`) — or `--only <name>` for a single
scenario.

**Not yet covered**, explicitly: a `cc_shared_library`-shaped
detection-correctness scenario (every current fixture pair is
`cc_binary(linkshared = True)`); the multi-library aggregate-plumbing and
consumer/app-scoped gaps are covered separately by `strings_lib/` and
`consumer/`'s own CI wiring, not by a scenario-manifest entry.

## Suppressions

`suppressions/` holds selector-based suppression fixtures, each with an
`expected_suppressed_count`/`expected_suppressed_symbols` the same
`scenarios.yml` run re-verifies on every scenario run.

## Scenario receipts

`scripts/emit_scenario_receipts.py` derives one receipt per header-frontend
profile (`detection-correctness-scenarios-{castxml,clang}`) from
`run_scenario.py`'s own `summary.json`: `passed` only if every result for
that profile passed, `failed` on any mismatch, `skipped` if the manifest
currently declares no scenario for that profile at all. `scenarios.yml`'s
own "Enforce gate" step validates these the same way `abi-scan.yml`
validates its own capability receipts — see
[canonical-bazel-gate.md](canonical-bazel-gate.md#capability-matrix-and-receipts).

## Canary behavior (`scenarios-canary.yml`)

`scenarios.yml` re-verifies every scenario/suppression, but only against
the pinned `abicheck` commit, and only when this repo's own
fixtures/scenarios/suppressions change. A suppression can go stale for a
reason that has nothing to do with a change in this repo: an upstream
`abicheck` release changes how a finding is classified or matched, and a
selector that used to match a real finding silently stops.
`scenarios-canary.yml` runs the identical suite against `abicheck/main`'s
current HEAD on a weekly schedule (plus on-demand via
`workflow_dispatch`), so staleness is caught independently of this repo's
own PR activity, before a scanner pin is ever bumped. It never gates a PR
and posts no PR comment — a red run in the Actions tab is the signal, and
it means "review before bumping the pin" (possibly a desired detector
improvement), not "this repo is broken."

## Capability matrix (`capabilities.yaml`)

The declarative, machine-checked source of truth for which validation axis
(evidence depth, header frontend, toolchain, target shape, comparison
scope) this repo's CI actually exercises, and which job exercises it.
`scripts/check_capability_matrix.py` validates every `covered`/
`non_gating_watch` entry's `job`/`workflow` against the real
`.github/workflows/*.yml` files, catching both a stale entry (points at a
job that no longer exists) and an undocumented one (a real job no entry
names). `scripts/gen_capability_gaps.py` generates the root README's
"Known limitations" gap list from this same file, so the two can't
disagree. `scripts/check_toolchain_matrix_sync.py` checks each
`toolchain_matrix` entry's `matrix_leg` against `abi-scan.yml`'s real
`strategy.matrix.include`.

## How to add a scenario

1. Create `fixtures/<name>/v1/` and `fixtures/<name>/v2/` as independent
   Bazel targets exercising the specific change you want a verdict for.
2. Add an entry to `scenarios/manifest.yaml` naming the fixture and its
   expected `abicheck compare` verdict.
3. Run `python3 scripts/run_scenario.py --only <name>` locally to confirm
   the actual verdict matches before opening a PR.
4. If the scenario is relevant to a specific header frontend or capability
   axis, consider whether `capabilities.yaml` needs a new or updated entry,
   and run `scripts/check_capability_matrix.py` to confirm consistency.
