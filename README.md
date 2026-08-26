# ABICheck Integration Lab

ABICheck Integration Lab is the executable acceptance and conformance
environment for [ABICheck](https://github.com/abicheck/abicheck) across
real C/C++ build systems, evidence producers, compatibility scenarios,
baselines, and GitHub Actions workflows. It is not a demo, not a starter
template, and not the main ABICheck implementation — it is where ABICheck's
claims about what it can detect, and how it integrates into a real CI
pipeline, get proven against real Bazel, CMake, and Make builds, real
scanner output, and real GitHub Actions runs, so that upstream changes can
be validated before other projects depend on them.

> **Current status.** Three paths run on every PR, and only one of them is
> branch-required:
>
> 1. **`abi-scan.yml` — required.** The real ABICheck scanner at
>    `depth: source` over the root Bazel build, resolved against the PR's
>    exact base commit. This is the one gate that can block a merge.
> 2. **`integration-shadow.yml` — advisory.** The same source built and
>    staged through Bazel, CMake and Make, each checked against its own
>    committed per-profile baseline. Real, and it does fail for real
>    reasons; it is not in branch protection.
> 3. **`project-shadow.yml` — advisory, promotion candidate.** The native
>    `.abicheck.yml` project contract: 18 required/deferred cells across
>    three contract profiles, an accepted-main baseline resolved with a
>    receipt, and a trailing aggregate gate. The explicit criteria for
>    making its aggregate branch-required are in
>    [docs/roadmap.md](docs/roadmap.md#promoting-the-native-project-aggregate).
>
> Everything below describes the current state. What is still being
> migrated — which profiles are contract profiles, which scenarios run
> under which build systems, what is still an expected gap — is stated in
> [Honest current limitations](#honest-current-limitations) and generated
> from `capabilities.yaml` and `scenarios/manifest.yaml` rather than
> hand-maintained here.

## Why this repository exists

`abicheck/abicheck` is the scanner: it defines evidence depths, comparison
semantics, suppression syntax, baseline formats, and the CLI/Action
surface. It is validated primarily against its own unit and integration
tests, on synthetic fixtures it controls end to end.

This repository answers a different question: **can ABICheck actually be
trusted and integrated into a real project's CI**, with a real Bazel
workspace, real GitHub Actions runners, real PR review flow, real
CODEOWNERS-protected baselines, and — increasingly — real CMake and Make
projects as well? It exists to catch the class of problem that only shows
up at the integration boundary: a coverage gap that reads as `COMPATIBLE`
at exit code 0, a PR-comment renderer that silently no-ops for one mode, a
Bazel evidence path that resolves zero targets while still reporting a
verdict, a suppression selector that goes stale against a new upstream
release. Findings here feed back into `abicheck/abicheck` — see
[UPSTREAM_TO_ABICHECK.md](UPSTREAM_TO_ABICHECK.md) for what has already
moved upstream and what remains lab-only.

## What is implemented today

**Native project integration shadow.** `.abicheck.yml` now declares the
customer-facing target/profile/check topology, and every profile producer
writes the upstream `abicheck.build-output/v1` format to
`abicheck-build-<profile>/build-output.json`. The former lab-specific receipt
is retained temporarily as `lab-build-output.json` only for the legacy parity
jobs. `.github/workflows/project-shadow.yml` validates the project and build
manifests with the upstream CLI, restores the exact PR-base `accepted-main`
baseline-set for every profile, then invokes upstream `check-project.yml` with
required/deferred target, consumer, plugin-contract, and bundle cells. The
established Bazel gate remains branch-required while the native aggregate earns
that repository-setting promotion.

The native topology now includes a real `core` provider consumed through an
internal C ABI by both `math` and `strings`, the three-member SDK bundle, the
executable consumer, and a math plugin symbol contract. The shadow asserts the
`DT_NEEDED` provider edges and runs the staged executable for all three GCC
build-system profiles. Product limitations that cannot yet be wired
truthfully—target-specific source-evidence routing, separate client compilers,
per-check runtime environments, and pybind cross-module internals—are recorded
as machine-readable `expected_gaps` in the scenario manifest instead of being
reported as covered.

`.github/workflows/project-baseline.yml` publishes generation-scoped
`accepted-main` baseline-sets to Actions cache on every main push, and every
pull-request project check resolves its baseline through
`ci/baseline_resolution.py`, which writes a **baseline-resolution receipt**
(uploaded as `baseline-resolution-<profile>`) naming every identity involved:

```json
{
  "requested_pr_base": "...",
  "selected_source_commit": "...",
  "cache_primary_key": "...",
  "cache_matched_key": "...",
  "manifest_project_ref": "...",
  "manifest_generation": 1,
  "reason_selected": "abi-equivalent-ancestor"
}
```

Three things can make the requested base and the baseline's own commit differ,
and the receipt distinguishes them rather than collapsing all three into one
`FileNotFoundError` on `baseline-set/manifest.json`:

- `exact-base` — the base commit's own published baseline was restored.
- `abi-equivalent-ancestor` — the base is separated from the restored
  baseline's commit only by commits touching nothing the ABI depends on.
  A bot-authored `chore: refresh ABICheck baseline` commit is the case that
  matters: it is pushed with automation credentials, so it starts no workflow
  run and can never own a baseline, and it rewrites only committed legacy
  `abi/` snapshots. The walk uses `git diff --no-renames` and stops at the
  first commit that changes anything else.
- `rebuilt-from-source-commit` — no usable baseline-set was in the cache, so
  one was rebuilt from the resolved source commit using *that commit's own*
  build recipe. Actions caches expire after seven idle days and are evicted
  under the repository's 10 GB budget, so a missing entry is routine; the
  accepted-main baseline is defined by its source commit, and the cache is
  transport. A rebuild is never receipted as a cache hit.

A prefix (`restore-keys`) match is a transport fallback only: the manifest's
`profile`, `project_ref`, and `baseline_generation` must still match exactly,
and an older unrelated cache is rejected as `wrong-project-ref` with both refs
printed. The staged result is uploaded under `check-project.yml`'s baseline
artifact convention. The run plan contains 18
required/deferred cells: three libraries, the consumer, the plugin contract,
and the SDK bundle across all three profiles; a negative aggregate oracle
proves that any missing required report fails coverage.

The one-time introduction PR has no baseline to resolve at all, because its
base commit predates the publisher workflow and so carries no `.abicheck.yml`
or profile build recipe to rebuild from either. The shadow detects that one
state from the base tree and runs build/manifest validation only. Every later
PR takes the resolution path above, which fails closed with a receipt when it
cannot produce an identity-matched baseline. Neither path ever manufactures a
PR baseline from the candidate under test: a rebuild always uses the resolved
accepted-main source commit's own tree, never this PR's.

**The canonical gate.** `.github/workflows/abi-scan.yml` runs the real
ABICheck scanner (`mode: scan`, `depth: source`) against this repo's root
Bazel build, resolves its trusted baseline from the pull request's exact
base commit (never the working tree), enforces an independent
evidence-coverage contract on top of the scanner's own verdict, and
produces one canonical report, one PR comment, and one pass/fail result.
This is the primary, required, load-bearing gate. Full detail:
[docs/canonical-bazel-gate.md](docs/canonical-bazel-gate.md).

**Multi-build-system profiles.** `ci/profiles.yaml` declares three
profiles, all building the same shared source tree
(`include/`, `src/`, `strings_lib/`, `consumer/`) rather than copies of it:

| Profile id | Build system | `contract` |
|---|---|---|
| `linux-x86_64-gcc14-cxx17-bazel` | Bazel | `true` |
| `linux-x86_64-gcc14-cxx17-cmake-ninja` | CMake + Ninja | `true` |
| `linux-x86_64-gcc14-cxx17-make-bear` | Make + required Bear evidence | `true` |

`.github/workflows/integration-shadow.yml` builds and stages all three on
every PR and push to `main`, runs a per-profile compatibility check
(`ci/check_profile.py`) against each profile's own committed baseline,
enforces a profile-specific coverage contract, emits a machine-readable
receipt per profile, and runs a fail-closed fan-in `integration_gate` job
over the expected receipts. A separate `cross_build_equivalence` job then
compares the three profiles' own staged output directly against each
other (exported symbols, SONAME, dynamic dependencies, headers), and a
`scenarios_cmake` job proves an initial subset of the scenario oracle
(`add_function`/`remove_function`) detects the same compatible/breaking
changes under CMake, not just Bazel. Despite the gate job's name, **this
entire workflow is advisory** — it is not in branch protection's
required-status-checks list, and nothing here can block a merge.
Two more workflows keep each profile's own baseline current:
`profile-baseline.yml` refreshes `abi/profiles/<id>/*.abicheck.json` on
every push to `main`, and `release.yml` re-certifies and publishes an
immutable per-profile baseline asset on every published release (today,
for every profile selected by the release policy). Full detail:
[docs/integration-profiles.md](docs/integration-profiles.md) and
[docs/operations.md](docs/operations.md#multi-build-system-profile-baseline-lifecycle).

**Scenarios and capabilities.** A machine-readable scenario oracle
(`scenarios/manifest.yaml` + `fixtures/*`) asserts abicheck's actual verdict
against known compatible/breaking changes; CastXML and Clang header-frontend
profiles cross-check each other; suppression tests catch stale selectors;
consumer-scoped and aggregate multi-library checks exercise `--used-by` and
`abicheck aggregate` against real targets; a declarative capability matrix
(`capabilities.yaml`) tracks which validation axis is actually covered by a
real job, with per-run receipts proving that job executed; a weekly canary
re-runs the scenario suite against `abicheck/main` to catch upstream drift
before a version pin is bumped; a broader scanner-candidate canary
(`canary.yml`) certifies a *specific* `abicheck` commit — dispatched by
`repository_dispatch`, on demand, or weekly — against the scenario oracle,
the real composite Action surface, and every multi-build-system profile,
classifying the result into one of five outcomes; cold/warm performance
measurements track Bazel and abicheck cache speedups, plus a per-profile
build/dump measurement for the cmake and make profiles; a committed
baseline lifecycle (`baseline.yml`) keeps the trusted comparison target
current. Full detail:
[docs/scenarios-and-capabilities.md](docs/scenarios-and-capabilities.md)
and [docs/integration-profiles.md](docs/integration-profiles.md#scanner-candidate-certification-canary).

The scenario oracle also contains two evidence/deployment demonstrations that
run as real binaries rather than mocked report fixtures. The
`l2-green-l4-macro-break` scenario proves an unchanged binary and L2 header
result becomes `API_BREAK/public_macro_value_changed` at source depth. The
`runtime-floor-raised` scenario uses a locally versioned `LABRT_1.0`/`2.0`
provider and proves the same candidate is risk without a deployment matrix,
breaking on the 1.0 tier, and floor-compatible on the 2.0 tier. The Clang
plugin lane now explicitly requests source depth and rejects any produced
report whose effective depth or assurance does not prove L4 consumption.
The Python lane builds a real scikit-build-core/pybind11 wheel and then proves
that byte-identical `_core` extension modules still produce an `API_BREAK`
when the adjacent `.pyi` renames a keyword and removes its default. This keeps
native extension ABI evidence distinct from the Python API contract while the
pybind cross-module internals identity remains an explicit expected gap.

## Honest current limitations

- **The advisory integration gate is not a branch-protection gate.** It can
  fail for real reasons and still not block a merge, by design, until it
  earns that trust.
- **The per-profile checker runs the real ABICheck scanner for CMake and
  Make, not yet for Bazel's own leg of this advisory workflow.**
  `ci/check_profile.py` (via `ci/real_scan.py`) runs an actual
  `abicheck dump --depth source` / `abicheck compare` for the
  `cmake`/`make` profiles, using each profile's own `compile_commands.json`
  as `--build-info`, filtered to the target's own translation unit. For
  the `bazel` profile's leg of *this same advisory workflow*, it still
  diffs ELF dynamic symbols, symbol kinds and ABI-relevant data sizes,
  SONAME/loader filenames, and public-header content digests — a real
  signal, but one that **cannot** classify a struct/class layout change, a
  default-argument change, an inline/template body change, or any other
  source-level API change invisible at the binary symbol-table level.
  Bazel doesn't need the real-scanner leg here because it already has its
  own real, required gate in the separate `abi-scan.yml`.
- **Cross-build-system equivalence checking exists but is advisory only.**
  `ci/compare_build_outputs.py` (the `cross_build_equivalence` job)
  compares each pair of profiles' own staged output directly — exported
  symbols, SONAME, dynamic dependencies, public headers — and classifies
  every difference as a real public-contract mismatch or expected
  build-system bookkeeping. It is real (run against this repo's own three
  profiles, it found and this repo then fixed a genuine SONAME mismatch —
  see
  [integration-profiles.md](docs/integration-profiles.md#cross-build-system-equivalence));
  a separate, still-open `abicheck_cross_check` finding (a real
  `abicheck compare` between Bazel's and CMake/Make's own built
  `libmath.so` reports `COMPATIBLE_WITH_RISK`, a DWARF/source-level
  difference the SONAME/symbol-table diff can't see on its own) remains.
  Like every other advisory job, none of this can block a merge.
- **Not every profile has a production release baseline.** Semantic
  scenario parity across build systems is now asserted rather than
  assumed: `scenario_parity` runs the suite under Bazel, CMake and Make
  and fails unless the same mutation yields the same normalized findings
  and verdict everywhere it ran (`scripts/check_scenario_parity.py`).
  Provenance — paths, timings, the producing build system's own
  bookkeeping — is excluded from that comparison by construction.
  `generated_header_removed_function` stays Bazel-only: its header comes
  from a Bazel genrule rather than the fixture directory, so the generic
  CMake/Make recipes have nothing to compile against. It fails closed as
  "no build mapping declared" rather than being skipped quietly.
- **Make source evidence is fail-closed.** Bear and the generated
  `compile_commands.json` are mandatory for the Make contract profile; a
  missing tool, failed capture, or a compile database whose entries are not
  well-typed fails the profile before comparison rather than degrading to a
  binary-only green result.
- **The producer-compiler axis is exercised by scenario, not yet by
  profile.** `depth-scenarios.yml`'s `producer-compiler` job builds one
  fixture under gcc-14 and clang-18 and asserts each producer's own
  findings: the GCC side must name the exact symbol pair the width change
  produces, the Clang side must name nothing. It does **not** hand ABICheck
  a profile id, so it does not prove the native project path routes a
  per-profile cell's findings to the right profile — that is the
  `producer-attribution-through-project-path` expected gap. The
  `linux-x86_64-clang18-cxx17-cmake-ninja` profile is declared but is
  `contract: false` and not yet scheduled by `ci/event-policy.yaml`: a
  profile earns scheduling once `profile-baseline.yml` has published
  `abi/profiles/<id>/` for it, and until then adding it to an event set
  would make the fan-in gate red for a missing baseline rather than a real
  finding.
- **Wheel comparison covers everything except post-repair.**
  `depth-scenarios.yml`'s `python-wheel` job builds both wheels and runs
  `abicheck compare old.whl new.whl`, asserting extension and `.pyi`
  discovery inside the package, the Python API findings, bundled native
  library discovery, package-level added/removed extensions, and wheel
  tags/CPython ABI metadata. The separate `python-extension` job compares a
  standalone `_core` extension against adjacent stubs — a fast unit-level
  acceptance case, not wheel integration, and described as such.
  Post-repair behaviour, where auditwheel/delocate/delvewheel rewrites
  RPATHs and vendors libraries, is still
  [roadmap item 10](docs/roadmap.md#declared-follow-ups).

See [docs/roadmap.md](docs/roadmap.md) for what closes these gaps, and in
what order.

### Known gaps (generated from `capabilities.yaml`)

This block is generated by `scripts/gen_capability_gaps.py --write` from
`capabilities.yaml`'s own `gap`/`planned` entries, and checked against it in
CI (`scripts/gen_capability_gaps.py --check`) — it cannot drift from what
the capability matrix actually declares.

<!-- capability-matrix:gaps:start -->
_No `gap`/`planned` entries are currently declared in `capabilities.yaml`._

Expected gaps from `scenarios/manifest.yaml` -- scenarios that run and are expected to fall short, with the upstream issue and the phase they fall short in, rather than being reported as covered:

- **plugin-pack-reuse-scope-contract** (`expected_gap`): falls short at `plugin-pack-reuse` -- `baseline_and_reuse_dump_scope_fingerprints_differ` (upstream: `integration-lab#pack-reuse-scope-contract`)
- **replay-report-assurance-block** (`expected_gap`): falls short at `plugin-pack-reuse` -- `replay_report_carries_no_analysis_assurance` (upstream: `abicheck#scan-report-analysis-assurance`)
- **plugin-pack-producer-identity** (`expected_gap`): falls short at `plugin-pack-reuse` -- `pack_manifest_records_no_producer_identity` (upstream: `integration-lab#pack-producer-identity`)
- **producer-attribution-through-project-path** (`expected_gap`): falls short at `check-project` -- `clang_profile_not_a_contract_profile` (upstream: `abicheck#per-profile-cell-attribution`)
- **same-binary-clang-client-only-break** (`expected_gap`): falls short at `check-project` -- `consumer_compile_not_applied` (upstream: `abicheck#consumer-compile-execution`)
- **per-check-runtime-environment** (`expected_gap`): falls short at `project-plan` -- `environment_selector_not_supported` (upstream: `abicheck#per-cell-environment`)
- **pybind-cross-module-internals** (`expected_gap`): falls short at `compare` -- `binding_internals_identity_not_collected` (upstream: `abicheck#binding-abi-provider`)
- **target-specific-source-evidence-routing** (`expected_gap`): falls short at `check-project` -- `target_evidence_path_not_projected` (upstream: `abicheck#run-plan-build-output-projection`)
<!-- capability-matrix:gaps:end -->

## Architecture

Three paths run on every PR. Only the first is branch-required today; the
promotion criteria for the third are in
[docs/roadmap.md](docs/roadmap.md#promoting-the-native-project-aggregate).

```text
                         ┌──────────────────────────────┐
                         │  pull_request / push:main     │
                         └───────────────┬──────────────┘
                                         │
        ┌────────────────────────┬───────┴────────┬────────────────────────┐
        │ REQUIRED, LOAD-BEARING │    ADVISORY    │  ADVISORY (native,     │
        │                        │                │  promotion candidate)  │
        ▼                        ▼                ▼
 .github/workflows/     .github/workflows/   .github/workflows/
    abi-scan.yml       integration-shadow.yml  project-shadow.yml
 ┌──────────────────┐  ┌─────────────────────┐ ┌───────────────────────────┐
 │ real abicheck     │  │ ci/select_profiles  │ │ .abicheck.yml (topology)   │
 │ scan, depth:source│  │ (profiles.yaml +    │ │   │                        │
 │ base-SHA baseline │  │  event-policy)      │ │ baseline-ref               │
 │ coverage contract │  │   bazel cmake make  │ │   ci/baseline_resolution   │
 │ ── ONE verdict ── │  │   ci/run_profile.py │ │   │  (receipt: which       │
 └──────────────────┘  │   → abicheck-build- │ │   │   commit, which key)   │
                        │      <id>/ (staged) │ │ build (3 profiles)         │
                        │   → check_profile   │ │   → abicheck-build-<id>/   │
                        │   → coverage        │ │ restore-baseline           │
                        │   → profile receipt │ │   cache hit, ABI-equivalent│
                        │   integration_gate  │ │   ancestor, or rebuild     │
                        └─────────────────────┘ │   from the source commit   │
                                                 │ project (upstream          │
   scenarios.yml / scenarios-canary.yml          │   check-project.yml)       │
   depth-scenarios.yml                           │   plan → check cells →     │
   (scenario oracle, capability matrix,          │   reports → aggregate      │
    suppressions, consumer/aggregate checks)     │ plan-oracle                │
                                                 │   run-plan semantics +     │
                                                 │   negative aggregate proof │
                                                 └───────────────────────────┘
```

Every ABICheck reference in every workflow resolves to the one reviewed pin in
[`ci/abicheck-version.yaml`](ci/abicheck-version.yaml). GitHub cannot
interpolate a `uses:` ref from a file, so reusable-workflow references are
written out literally — `tests/test_abicheck_pin.py` fails if any of them
drifts from the reviewed value.

## The project under test

A small public C++ project, built unchanged (not copied) by all three build
systems. `.abicheck.yml` is the authoritative topology; this is what it
declares:

| Target | Kind | Sources | Role |
|---|---|---|---|
| `core` | library | `core_include/`, `internal/` | The native provider both libraries consume through an internal C ABI. A cross-DSO provider signature change is therefore a real break in its consumers, not just in itself. |
| `math` | library | `include/`, `src/` | The primary public library. |
| `strings` | library | `strings_lib/` | A second, independent public library. |
| `consumer-app` | app-consumer | `consumer/` | A real executable linking `math` and calling only a subset of its API, so consumer-scoped analysis has something narrower than the full export table to scope to. |
| `math-plugin-contract` | plugin-contract | `plugins/contracts/math-plugin.syms` | The symbol contract a plugin host must keep, checked independently of `math`'s own surface. |
| `sdk` | bundle | `core` + `math` + `strings` | The three-member release bundle, checked as one unit as well as per member. |

Each of those is checked on all three contract profiles, giving the 18
required/deferred cells the native run plan generates.

Alongside the C++ targets: `bindings/python/` is a real
scikit-build-core/pybind11 extension (`abicheck_lab_py`) with committed
`.pyi` stubs, and `//:math_shared` is a `cc_shared_library`-shaped variant
of `math` kept so that target shape is exercised too.

Test PRs intentionally exercise compatible additions, binary ABI breaks,
source/API breaks, and implementation-only changes against the canonical
gate — some are intentionally kept open as reusable acceptance cases rather
than merged or closed; see
[docs/operations.md](docs/operations.md#long-lived-intentional-test-prs).
Those branches had drifted from what they claim to demonstrate. They are now
declared in [`demos/manifest.yaml`](demos/manifest.yaml) as *base + one
patch* plus the result each must produce, regenerable with
`scripts/gen_demo_prs.py`, and checked by the gating `demo_oracle` job, which
reads the gate's own report and passes only when the natural result is the
declared one. `scripts/gen_demo_prs.py --check` reports which branches are
stale; the force-push that refreshes them is a deliberate operator step.

## Canonical staged-output shape

Every multi-build-system profile stages into
`abicheck-build-<profile-id>/`, the one shape every downstream consumer
(checker, coverage contract, receipt) reads regardless of backend:

```text
abicheck-build-<profile-id>/
├── build-output.json   # profile id, project identity, targets, digests
├── artifacts/            # built shared libraries / binaries
├── headers/               # staged public headers
├── evidence/                # backend-specific compile evidence
├── provenance/                # build command, environment, toolchain info
```

## Local quick start

```bash
# Canonical Bazel build + real ABICheck scan (needs bazel and network access
# to install abicheck):
bazel build //:math
pip install "abicheck @ git+https://github.com/abicheck/abicheck.git@6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"
abicheck scan --sources . --depth source --against abi/math.abicheck.json

# One multi-build-system profile: build, stage, and validate build-output.json
# only (ci/run_profile.py stops there — it does NOT run check_profile.py's
# ABI signal, the coverage contract, or emit a receipt; those are separate
# workflow steps, run manually below or via integration-shadow.yml):
python3 ci/run_profile.py --profile-id linux-x86_64-gcc14-cxx17-bazel
python3 ci/run_profile.py --profile-id linux-x86_64-gcc14-cxx17-cmake-ninja
python3 ci/run_profile.py --profile-id linux-x86_64-gcc14-cxx17-make-bear

# To also run the per-profile ABI signal against a committed baseline
# (bazel: the lab-only ELF signal, needs only nm/readelf; cmake/make: the
# REAL abicheck scanner via ci/real_scan.py, needs the same pinned
# abicheck + CastXML install as above -- see .github/workflows/
# integration-shadow.yml's "Install real abicheck scanner + pinned
# CastXML" step for the exact commands):
python3 ci/check_profile.py check --profile-id linux-x86_64-gcc14-cxx17-bazel \
  --target math --staged-dir abicheck-build-linux-x86_64-gcc14-cxx17-bazel \
  --baseline abi/profiles/linux-x86_64-gcc14-cxx17-bazel/math.abicheck.json \
  --out report-math.json

# Scenario oracle (needs bazel + abicheck on PATH):
python3 scripts/run_scenario.py
```

See each profile's `build_command` in `ci/profiles.yaml` for the exact
underlying build invocation, and `ci/profiles.yaml`'s own header comment
for the gcc-14/g++-14 toolchain this lab pins to (installable via
`apt-get install -y gcc-14 g++-14` where not already present).

## Adding a profile or scenario

- **A new build profile** (a new build system, compiler, or standard): see
  [docs/integration-profiles.md#adding-a-profile](docs/integration-profiles.md#adding-a-profile).
  Short version: declare it in `ci/profiles.yaml`, implement a backend if
  needed, wire it into `ci/event-policy.yaml`, seed a baseline, and run it
  locally before opening a PR.
- **A new compatibility scenario** (proving a specific detected/undetected
  change): see
  [docs/scenarios-and-capabilities.md#how-to-add-a-scenario](docs/scenarios-and-capabilities.md#how-to-add-a-scenario).
  Short version: add `fixtures/<name>/{v1,v2}/`, add an entry to
  `scenarios/manifest.yaml` with the expected verdict, and verify locally
  with `scripts/run_scenario.py --only <name>`.

## Documentation map

| Document | Covers |
|---|---|
| [docs/canonical-bazel-gate.md](docs/canonical-bazel-gate.md) | The required `abi-scan.yml` gate: baseline resolution, PR comments, coverage enforcement, Bazel evidence, consumer/aggregate/toolchain checks, determinism, capability receipts. |
| [docs/integration-profiles.md](docs/integration-profiles.md) | Profile definitions, event policy, backends, staged output, per-profile checks and their limits, receipts, the advisory gate, cross-build-system equivalence, planned migration. |
| [docs/scenarios-and-capabilities.md](docs/scenarios-and-capabilities.md) | The scenario oracle, suppressions, receipts, the capability matrix, canary drift detection, how to add a scenario. |
| [docs/operations.md](docs/operations.md) | Baseline refresh, branch protection, required checks, CODEOWNERS, permissions, caching, retention, scanner pinning, long-lived test PRs. |
| [docs/roadmap.md](docs/roadmap.md) | Planned phases, no dates, no completion claims. |
| [UPSTREAM_TO_ABICHECK.md](UPSTREAM_TO_ABICHECK.md) | What this lab has found that should move into `abicheck/abicheck`, and what remains lab-only by design. |

## Roadmap (short version)

Run the real scanner for CMake/Make profiles → consider replacing shadow
orchestration with a direct `check-target`/`check-project` invocation →
broaden cross-build-system equivalence checking (advisory version already
exists, promote to required once build definitions are aligned and
stable) and scenario-oracle build-system parity → promote proven profiles
to required contracts (accepted-main/release-contract baselines already
exist for the one profile that has one today) → scanner release-candidate
dispatch (`canary.yml`: done, see above) → expand toolchain/platform
coverage as real integration needs demonstrate them. Full detail, still
with no dates or completion claims:
[docs/roadmap.md](docs/roadmap.md).
