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
