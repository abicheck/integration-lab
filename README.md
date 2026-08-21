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

> **Current status: transition in progress.** The repository's one
> required, load-bearing compatibility gate is still the Bazel-based
> workflow described below. A newer, parallel multi-build-system system
> (Bazel + CMake + Make, all three staged and checked) exists, runs on
> every PR, and can genuinely fail — but it is advisory only and does not
> block merges yet. Its per-build-system compatibility check
> (`ci/check_profile.py`) is also a lab-only ELF/header signal, not the real
> ABICheck scanner, for **all three** profiles including Bazel — the real
> ABICheck scanner only runs in the separate, canonical `abi-scan.yml` gate.
> See [Honest current limitations](#honest-current-limitations) before relying
> on any of it as proof of anything beyond "the same source builds under
> three build systems."

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
| `linux-x86_64-gcc14-cxx17-cmake-ninja` | CMake + Ninja | `false` |
| `linux-x86_64-gcc14-cxx17-make-bear` | Make (+ Bear when available) | `false` |

`.github/workflows/integration-shadow.yml` builds and stages all three on
every PR and push to `main`, runs a per-profile compatibility check
(`ci/check_profile.py`) against each profile's own committed baseline,
enforces a profile-specific coverage contract, emits a machine-readable
receipt per profile, and runs a fail-closed fan-in `integration_gate` job
over the expected receipts. Despite that job's name, **this entire workflow
is advisory** — it is not in branch protection's required-status-checks
list, and nothing here can block a merge. Full detail:
[docs/integration-profiles.md](docs/integration-profiles.md).

**Scenarios and capabilities.** A machine-readable scenario oracle
(`scenarios/manifest.yaml` + `fixtures/*`) asserts abicheck's actual verdict
against known compatible/breaking changes; CastXML and Clang header-frontend
profiles cross-check each other; suppression tests catch stale selectors;
consumer-scoped and aggregate multi-library checks exercise `--used-by` and
`abicheck aggregate` against real targets; a declarative capability matrix
(`capabilities.yaml`) tracks which validation axis is actually covered by a
real job, with per-run receipts proving that job executed; a weekly canary
re-runs the scenario suite against `abicheck/main` to catch upstream drift
before a version pin is bumped; cold/warm performance measurements track
Bazel and abicheck cache speedups; a committed baseline lifecycle
(`baseline.yml`) keeps the trusted comparison target current. Full detail:
[docs/scenarios-and-capabilities.md](docs/scenarios-and-capabilities.md).

## Honest current limitations

- **The advisory integration gate is not a branch-protection gate.** It can
  fail for real reasons and still not block a merge, by design, until it
  earns that trust.
- **The per-profile checker is not the real ABICheck scanner.**
  `ci/check_profile.py` diffs ELF dynamic symbols, symbol kinds and
  ABI-relevant data sizes, SONAME/loader filenames, and public-header
  content digests. It reliably catches an exported symbol being
  added/removed/resized and a SONAME or header change. It **cannot**
  classify a struct/class layout change, a default-argument change, an
  inline/template body change, or any other source-level API change
  invisible at the binary symbol-table level — that requires the real
  ABICheck scanner, which this checker is not.
- **No cross-build-system equivalence checking exists yet.** Each profile
  is compared only against its own baseline, not against the other two
  profiles' own reports.
- **Not every profile has a production release baseline**, and not every
  scenario runs through every build system — the scenario oracle currently
  exercises Bazel only.
- **Make's `bear`-captured evidence is best-effort, not authoritative** —
  when `bear` isn't on `PATH`, the Make profile degrades to "no
  compile-commands evidence" rather than failing.

See [docs/roadmap.md](docs/roadmap.md) for what closes these gaps, and in
what order.

### Known gaps (generated from `capabilities.yaml`)

This block is generated by `scripts/gen_capability_gaps.py --write` from
`capabilities.yaml`'s own `gap`/`planned` entries, and checked against it in
CI (`scripts/gen_capability_gaps.py --check`) — it cannot drift from what
the capability matrix actually declares.

<!-- capability-matrix:gaps:start -->
_No `gap`/`planned` entries are currently declared in `capabilities.yaml`._
<!-- capability-matrix:gaps:end -->

## Architecture

```text
                         ┌─────────────────────────────┐
                         │   pull_request / push:main    │
                         └───────────────┬──────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │  REQUIRED, LOAD-BEARING  │   ADVISORY, NON-GATING     │
              ▼                          ▼                          │
   .github/workflows/          .github/workflows/                   │
      abi-scan.yml              integration-shadow.yml               │
   ┌──────────────────┐    ┌──────────────────────────────────┐     │
   │ real abicheck     │    │ ci/select_profiles.py             │     │
   │ scan, depth:source│    │   (profiles.yaml + event-policy)  │     │
   │ base-SHA baseline │    │        │        │        │        │     │
   │ coverage contract │    │     bazel    cmake     make        │     │
   │ ── ONE verdict ── │    │        │        │        │        │     │
   └──────────────────┘    │  ci/run_profile.py (per profile)   │     │
                            │  → abicheck-build-<id>/ (staged)   │     │
                            │  → ci/check_profile.py (ELF signal)│     │
                            │  → ci/check_profile_coverage.py    │     │
                            │  → ci/emit_profile_receipt.py      │     │
                            │        │        │        │        │     │
                            │        └────────┴────────┘        │     │
                            │      integration_gate (fan-in)     │     │
                            └──────────────────────────────────┘     │
                                                                      │
                            scenarios.yml / scenarios-canary.yml ─────┘
                            (scenario oracle, capability matrix,
                             suppressions, consumer/aggregate checks)
```

## The project under test

A small public C++ shared-library project: `//:math` (`include/`, `src/`),
a second independent library `//strings_lib:strings`, a real consumer
application `//consumer:consumer_app` that links against `//:math` and
calls only a subset of its API, and a `cc_shared_library`-shaped variant
`//:math_shared`. The same sources are also built by
`buildsystems/cmake/CMakeLists.txt` and `buildsystems/make/Makefile`
without copying anything. Test PRs intentionally exercise compatible
additions, binary ABI breaks, source/API breaks, and implementation-only
changes against the canonical gate — some are intentionally kept open as
reusable acceptance cases rather than merged or closed; see
[docs/operations.md](docs/operations.md#long-lived-intentional-test-prs).

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

# To also run the per-profile ABI signal against a committed baseline:
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
| [docs/integration-profiles.md](docs/integration-profiles.md) | Profile definitions, event policy, backends, staged output, per-profile checks and their limits, receipts, the advisory gate, planned migration. |
| [docs/scenarios-and-capabilities.md](docs/scenarios-and-capabilities.md) | The scenario oracle, suppressions, receipts, the capability matrix, canary drift detection, how to add a scenario. |
| [docs/operations.md](docs/operations.md) | Baseline refresh, branch protection, required checks, CODEOWNERS, permissions, caching, retention, scanner pinning, long-lived test PRs. |
| [docs/roadmap.md](docs/roadmap.md) | Planned phases, no dates, no completion claims. |
| [UPSTREAM_TO_ABICHECK.md](UPSTREAM_TO_ABICHECK.md) | What this lab has found that should move into `abicheck/abicheck`, and what remains lab-only by design. |

## Roadmap (short version)

Run the real scanner for CMake/Make profiles → consider replacing shadow
orchestration with a direct `check-target`/`check-project` invocation →
cross-build-system equivalence checking → promote proven profiles to
required contracts → accepted-main/release-contract baselines → scanner
release-candidate dispatch → expand toolchain/platform coverage as real
integration needs demonstrate them. Full detail, still with no dates or
completion claims: [docs/roadmap.md](docs/roadmap.md).
