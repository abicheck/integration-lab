# Multi-build-system integration profiles

This document explains the build-profile system: `ci/profiles.yaml`,
`ci/event-policy.yaml`, the backend abstraction, canonical staged output,
per-profile checks, receipts, and the advisory integration gate. See the
root [README.md](../README.md) for how this fits alongside the canonical
Bazel gate, and [roadmap.md](roadmap.md) for what's planned next.

**Current status: advisory.** Nothing described in this document blocks a
merge. The one required, load-bearing compatibility gate remains
`.github/workflows/abi-scan.yml`, documented in
[canonical-bazel-gate.md](canonical-bazel-gate.md).

## Profile definitions (`ci/profiles.yaml`)

A **build profile** is one explicit combination of build system + generator
+ compiler + compiler version + C++ standard + platform + evidence
mechanism. Three profiles are declared today, all built against the same
shared source tree (`include/`, `src/`, `strings_lib/`, `consumer/`) rather
than copies of it:

| Profile id | Backend | `contract` | Notes |
|---|---|---|---|
| `linux-x86_64-gcc14-cxx17-bazel` | Bazel | `true` | The existing, unchanged root Bazel build `abi-scan.yml` already gates on. |
| `linux-x86_64-gcc14-cxx17-cmake-ninja` | CMake + Ninja | `false` | `buildsystems/cmake/CMakeLists.txt` references the shared sources directly. |
| `linux-x86_64-gcc14-cxx17-make-bear` | Make | `false` | `buildsystems/make/Makefile`, with optional `bear`-generated `compile_commands.json`. |

`contract: true` marks the one profile whose result is trusted as a
required project contract today — nothing about that changes here.
`contract: false` (a **contract profile** is the term for one where this
flips) marks an **advisory profile**: failures are reported but never block
a merge.

Each profile also declares a `coverage` block (see below) and the exact
`cc`/`cxx`/`standard` it's pinned to — there is no silent fallback to
"whatever compiler happens to be on `PATH`".

## Event policy (`ci/event-policy.yaml`)

`ci/select_profiles.py` is the only thing that turns "an event fired" (pull
request, push to `main`, release, scheduled canary, manual dispatch) into a
list of profile ids to run, reading `profile_sets` (named groups of profile
ids) and per-event `required`/`advisory` lists from this file. Today every
event resolves the same thing: all three profiles, all advisory — the
`required`/`advisory` split is groundwork for a future promotion (see
[roadmap.md](roadmap.md)), not an enforcement mechanism in effect today.

## Backend abstraction (`ci/backends/`)

`ci/backends/{bazel,cmake,make}.py` each implement a common interface
(`base.py`): `verify_environment()`, `build()`, `collect_evidence()`,
`stage()`. `ci/run_profile.py` drives one profile's backend through a real
build and hands the result to `ci/emit_build_output.py`.

## Canonical staged output

Every profile stages into `abicheck-build-<profile-id>/`, validated against
`ci/schemas/build-output.schema.json` via `ci/validate_build_output.py`:

```text
abicheck-build-<profile-id>/
├── build-output.json      # profile id, project identity, targets, digests
├── artifacts/              # built shared libraries / binaries
├── headers/                 # staged public headers
├── evidence/                 # backend-specific compile evidence
│                              # (Bazel: cquery/aquery results;
│                              #  CMake/Make: compile_commands.json)
└── provenance/                # build command, environment, toolchain info
```

`build-output.json` is the one canonical, cross-backend shape every
downstream consumer (per-profile check, coverage contract, receipt) reads —
a CMake build and a Bazel build both stage into the identical structure,
even though what populates `evidence/` differs per backend.

## Profile-specific coverage (`ci/check_profile_coverage.py`)

A **coverage contract**: independent validation that the evidence requested
by a check was actually collected and belongs to the artifact/profile being
checked. Per profile: the candidate artifact exists and its digest matches
what was staged, public headers are actually present
(`require_public_headers`), and a backend-appropriate compile-evidence
signal was collected — Bazel: `min_resolved_targets` via `bazel cquery`;
CMake/Make: `min_compile_units` via a non-empty `compile_commands.json`
(`require_compile_commands: false` on the Make profile, since `bear` is
optional there).

## The current lab checker and its limitations (`ci/check_profile.py`)

**This is the single most important limitation to understand about this
whole system.** `ci/check_profile.py` is a lab-specific compatibility
signal, not the real ABICheck source-level scanner. It diffs each staged
shared library's:

- ELF dynamic symbol table (`nm -D`/`readelf`) — symbols added/removed;
- symbol kinds and ABI-relevant data sizes;
- SONAME (or, absent one, the loader-visible on-disk filename);
- public-header content digests (surfaced as a `public_headers_changed`
  note, never silently dropped).

It reliably catches an exported symbol being removed or resized, a SONAME
change, or a header content change. **It cannot** classify a struct/class
layout change with no symbol rename, a default-argument change, an
inline/template body change, or any other source/API change invisible at
the binary symbol-table level. When headers changed and nothing else did,
the verdict is `NOT_COMPARABLE` — reported as unclassified, not as a proven
compatible change. See that script's own module docstring for the full
accounting.

`dump`/`check` modes write and compare against
`abi/profiles/<profile-id>/{math,strings}.abicheck.json` — a baseline
schema and mechanism entirely separate from `abi/math.abicheck.json`, which
`abi-scan.yml`'s real scan still reads unchanged.

## Profile receipts (`ci/emit_profile_receipt.py`)

A **profile receipt** is a machine-readable record of an expected profile's
build attempt and result for the current workflow run — schema
`abicheck.integration-profile-receipt/v1`. One receipt is emitted per
profile per run, always, even when the profile's own build failed and
produced no `build-output.json` at all (that case's receipt reports
`status: failed`, not a missing receipt). A receipt's existence is not by
itself proof the profile ran successfully — read its `build.success` field,
the validated artifact digests, and the per-target reports for that: build/
check/coverage status, per-target report digests and verdicts, build-output
digest, and a scanner-mechanism note (so a receipt can never be mistaken for
evidence from the real scanner).

## The advisory integration gate

The `integration_gate` job in `.github/workflows/integration-shadow.yml`
runs after every profile's build+check+receipt steps, downloads every
profile's receipt, validates each against its schema and against the set of
profiles actually expected for this event, and requires `status: passed`
with clean per-target verdicts. It fails closed on a missing, invalid, or
`BREAKING`/`NOT_COMPARABLE` receipt — this job is real, and can and does
fail — but despite its name, **it is not the repository's required
branch-protection gate**. It is not in the required-status-checks list, and
a CMake or Make build failure (or a red `check_profile.py` result) cannot
block a merge today. Only `abi-scan.yml`'s Bazel `scan` job can.

## Cross-build-system equivalence

Every per-profile check above (`ci/check_profile.py`) answers "does this
profile's own build still match its own baseline" — it says nothing about
whether Bazel's, CMake's, and Make's own builds of the *identical* source
agree with *each other*. `ci/compare_build_outputs.py` answers that
question directly: for every pair of staged profiles built from the same
commit, it compares the built library's exported dynamic symbol set,
SONAME, `NEEDED` dynamic dependencies (`nm -D`/`readelf`), and the staged
public-header inventory and content digest — reusing `ci/check_profile.py`'s
own extraction functions rather than duplicating them.

Every difference is classified into one of five categories, so a real
divergence never gets lost among expected build-system bookkeeping:

| Category | Meaning |
|---|---|
| `public_contract_mismatch` | A real ABI-relevant difference — the finding this check exists to catch. |
| `toolchain_configuration_mismatch` | Same pinned toolchain intent, different link/compiler behavior (e.g. differing `NEEDED` entries from linker defaults). |
| `expected_build_system_metadata_difference` | Target ids, generator, build command, root paths — always differ, never a finding on their own. |
| `evidence_only_difference` | Evidence-producer path/location differences (`compile_commands.json` location, a bazel-query dir, ...). |
| `unclassified_difference` | Anything this script can't place elsewhere — currently only a staged public-header content mismatch, which should be structurally impossible (both sides copy the exact same repo file) and likely indicates a staging bug. |

Only each pair's *shared* `coverage.checked_targets` (from `ci/profiles.yaml`)
is compared — not every target either side's `build-output.json` happens to
mark `shared_library`. This matters concretely: the Bazel profile also
builds `//:math_shared`, a real `cc_shared_library` alongside `//:math`'s
`cc_binary(linkshared = True)` demo shape — a deliberately Bazel-only
capability (see "Bazel-only diagnostics may remain Bazel-specific" in the
design's own guidance) that must never be reported as "one profile built a
target the other didn't."

When the real `abicheck` CLI happens to be on `PATH` in the run
(`.github/workflows/integration-shadow.yml`'s `cross_build_equivalence` job
installs it best-effort), this script also runs one real `abicheck compare`
directly between each pair's own built library, as a deeper signal beyond
the symbol-table diff — it can notice a struct-layout or default-argument
difference the ELF-level diff structurally cannot. It records `not_run`,
never a silent "equivalent", when the CLI isn't reachable.

**A real finding, left as a finding, not "fixed" here:** run against this
repo's own three profiles, this check found that Bazel's `//:math` sets no
SONAME at all, CMake's build sets `libmath.so.1`, and Make's build sets
`libmath.so` — three different answers for the identical source. That's
exactly the class of divergence this check exists to surface; aligning it
is a build-definition decision for a future change, not something this
comparison tooling should paper over.

`cross_build_equivalence` runs this after every profile's build, uploads
`cross-build-equivalence.{json,md}`, and `integration_gate` notes its
result in the job summary — advisory only, same as everything else in this
document, until the build definitions are intentionally aligned and
stable (see [roadmap.md](roadmap.md)).

## Scanner candidate certification (canary)

`.github/workflows/canary.yml` (PR5, design doc section 28) certifies a
specific `abicheck` commit — not just whatever's pinned in `abi-scan.yml`
today — against every layer this lab can actually exercise, dispatched
three ways: a weekly `schedule` (certifies `abicheck/main`), a manual
`workflow_dispatch` (any branch/tag/SHA), or a `repository_dispatch` of
type `scanner-candidate` carrying `{"scanner_repository":
"abicheck/abicheck", "scanner_sha": "<exact commit SHA>",
"scanner_version": "<optional>", "baseline_generation": "<optional>"}` —
the shape `abicheck`'s own release automation would send ahead of cutting
a release. `scripts/resolve_scanner_candidate.py` validates that payload
(a missing or non-40-hex-character `scanner_sha` is rejected outright,
before any certification work runs) and resolves a `workflow_dispatch`/
`schedule` ref to an exact SHA via `git ls-remote`, so every job downstream
of `resolve_candidate` certifies exactly one pinned commit.

This is a separate, broader workflow from the pre-existing
`scenarios-canary.yml` (unchanged by PR5, still the narrower "does
`abicheck/main` still agree with our Bazel-only scenario oracle" weekly
check it always was). `canary.yml` runs three independent layers against
the resolved candidate:

| Job | Layer | What it exercises |
|---|---|---|
| `semantic_canary` | detector correctness | `scenarios/manifest.yaml`'s fixture-pair oracle, matrixed over both build systems `scripts/run_scenario.py` supports (bazel, cmake) — the same mechanism `scenarios-canary.yml` uses, extended to cmake and to the resolved candidate instead of always `main`. |
| `action_contract_canary` | GitHub Action surface | the real `abicheck/abicheck` composite Action, checked out at the candidate SHA and invoked via `uses: ./...` (a local path, not a dynamic `owner/repo@${{ expr }}` step reference) — a self-comparison (the reference Bazel `//:math` build against a fresh dump of itself) exercising the Action's own input/output/exit-code contract. |
| `build_integration_canary` | multi-build-system integration | the candidate's own CLI (`abicheck compare`), matrixed over all three `ci/profiles.yaml` profiles, each a self-comparison of that profile's own staged `math` library. |

Every layer's actual ABI assertion is `abicheck compare` (a self-comparison:
an unchanged library+header against itself, or a fresh dump against the
build it was dumped from) — deliberately never `abicheck scan --against
<a dump-mode baseline>`. See `UPSTREAM_TO_ABICHECK.md`'s own P0 entry: that
exact combination is a confirmed, deterministic upstream bug (a
comparability-field divergence between `scan` mode's and `dump` mode's own
candidate construction) that reports `NOT_COMPARABLE` for byte-identical
inputs, independent of which commit is pinned — a canary built on that
pattern would misreport every candidate as broken, forever.

`classify`, the final fan-in job, turns every layer's own job `result`
into exactly one of five outcomes (`scripts/classify_canary_outcome.py`,
design doc section 24): `INFRASTRUCTURE_FAILED` (a setup/environment
problem, never a detector signal — and this always wins over the others,
including when a later layer also failed, since an infra failure upstream
routinely cascades into "downstream step skipped"), `DETECTOR_DRIFT`
(the semantic layer), `ACTION_CONTRACT_DRIFT` (the Action-surface layer),
`BUILD_INTEGRATION_DRIFT` (the multi-build-system layer), or `SUCCESS`.
This classification is deliberately coarse — one signal per job, not per
step — a real scoping limitation versus a finer step-level breakdown; see
that script's own module docstring for the exact priority rules. Advisory
only, like every other workflow in this document: `canary.yml` is not a
required status check, and cannot be — it never runs against a PR's own
commit in the first place.

## Planned migration to the real ABICheck Action

`ci/check_profile.py`'s ELF/header signal exists only because the real
`abicheck` scanner isn't installable in this lab's own validation sandbox
(no network access there). A real CI run does have network access. The
planned migration (see [roadmap.md](roadmap.md)) replaces
`check_profile.py`'s comparison mechanism with a real `abicheck
scan`/`compare` invocation for the CMake and Make profiles — CMake's
`compile_commands.json` and Make's `bear`-generated equivalent are both
legitimate `--build-info`-shaped inputs once wired up — while keeping the
profile/receipt/gate architecture built around it. Promoting any profile
from advisory to `contract: true` is a separate, later decision, not
implied by this migration alone.

## Adding a profile

1. Add an entry to `ci/profiles.yaml`: an id following the
   `<os>-<arch>-<compiler+version>-cxx<standard>-<backend>` scheme, its
   `backend`, `contract: false` (advisory, almost always the right default
   for a new profile), targets, header roots, build command, and a
   `coverage` block.
2. If the backend is new, implement `ci/backends/<name>.py` against the
   common interface in `ci/backends/base.py`.
3. Add the profile id to the relevant `profile_sets`/event lists in
   `ci/event-policy.yaml`.
4. Seed a baseline: `python3 ci/check_profile.py dump --profile-id <id>
   --target <name> --staged-dir abicheck-build-<id> --out
   abi/profiles/<id>/<name>.abicheck.json` after a clean local build.
5. Run `python3 ci/run_profile.py --profile-id <id>` locally to confirm the
   backend builds, stages, and checks cleanly before opening a PR.
