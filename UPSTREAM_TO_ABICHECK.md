# What should move from `abicheck-bazel-lab` into `abicheck`

**Lab revision reviewed:** `napetrov/abicheck-bazel-lab@d15f999c036711ac0f1f6d4399632ffabbff8eb9`  
**Lab migration target:** `abicheck/abicheck@6dadbe82141e64c7fe3d12b363e2ee2c78b4264b` (`main`, 2026-08-14)

The lab is now more than an example repository. It contains working reference implementations for Bazel target discovery, source-fact collection, coverage assurance, producer conformance, trusted baselines, multi-target aggregation, consumer-scoped checking, scenario oracles, compiler matrices, and performance measurement.

The intended product boundary is:

- the lab declares small targets, profiles, fixtures, and expected outcomes;
- `abicheck` owns collection, normalization, comparability, evaluation, reporting, and CI gating;
- normal users must not copy several hundred lines of workflow and lab-specific Python to get the reliable behavior demonstrated here.

## Already available in the migrated `abicheck/main`

These lab workarounds are now upstream and should be consumed rather than extended locally:

1. Native sticky PR comments for single-artifact `scan --against` reports.
2. Supplemental Bazel `cquery` during zero-config `--sources` collection.
3. Backend-independent `canonical_finding_id` and `finding_id:` suppression selectors.
4. `NEW_TARGET` / `allow-new-target` baseline lifecycle handling.
5. Prebuilt Clang-plugin artifact inputs with SHA-256 verification and safer Intel LLVM handling.

The migration therefore enables native `scan` comments, removes the custom poster, replaces the old scanner pins, and removes the duplicate PR canary. The remaining items below are the capabilities still demonstrated only—or more completely—in the lab.

---

# P0: correctness, security, and fail-closed behavior

## P0.1 Cross-producer constructor/destructor identity reconciliation

### Lab evidence

An unchanged constructor may be represented by CastXML as a synthetic key such as:

```text
__abicheck_ctor__Calculator()
```

while direct Clang uses a different declaration identity. A CastXML baseline compared with a Clang candidate has produced a Clang-only `func_removed` and a false `BREAKING` verdict.

`canonical_finding_id` is too late to solve this: it identifies an already-created finding, while the false finding is created during old/new declaration matching.

### Required upstream behavior

Add an ambiguity-safe constructor/destructor reconciliation tier after exact-key and real-mangled-name matching:

- qualified owner type;
- constructor versus destructor kind;
- canonical parameter types;
- copy/move/converting-constructor distinction;
- CV/ref/variadic/template identity where applicable;
- exactly-one-candidate rule;
- explicit match provenance;
- both sides marked consumed, preventing removal-plus-addition duplication.

Do not use broad display-name matching to merge distinct real mangled symbols.

### Acceptance criteria

An unchanged default constructor produces no removal in CastXML, direct-Clang, replay, or plugin profiles. A genuinely removed overload still reports a break. Ambiguous overload sets remain unmatched rather than guessed.

---

## P0.2 Explicit Bazel root-target scoping

### Lab evidence

The lab must run target-scoped queries itself:

```text
bazel cquery --output=jsonproto 'deps(//:math)'
bazel aquery ... 'deps(//:math)'
```

Using `deps(//...)` pulled unrelated fixture TUs into the `//:math` evidence pack. Changed-scope replay then selected irrelevant units and reduced source/export coverage.

Latest main now runs both `cquery` and `aquery`, but still has no requested-root-target input and still queries the whole workspace.

### Required upstream surface

Normalize these interfaces into one typed request:

```yaml
build_system: bazel
build_targets:
  - //:math
```

Support it in:

- `dump` and `scan`;
- the root GitHub Action;
- `collect-facts`;
- `check-target` / `check-project`;
- `.abicheck.yml` and run-plan cells;
- Python/service API.

Report requested roots separately from their transitive closure:

```json
{
  "root_targets_requested": ["//:math"],
  "root_targets_resolved": ["//:math"],
  "transitive_target_count": 31,
  "compile_unit_count": 1,
  "link_unit_count": 1
}
```

Unknown roots must fail clearly instead of silently falling back to `//...`. Root identity must participate in profile/comparability fingerprints.

### Acceptance criteria

Changing an independent fixture target does not alter selected TUs, graph nodes, export coverage, or the `//:math` snapshot.

### Lab code removable afterward

- manual `cquery` / `aquery` workflow steps;
- `scripts/build_bazel_evidence_pack.py` for normal scans;
- lab-side Bazel evidence-summary parsing.

---

## P0.3 Apply L3 build context to L2 public-header parsing

**Root cause traced to a specific, already-acknowledged gap upstream
(2026-08-15 follow-up audit).** Confirmed against `abicheck/abicheck`
directly, not by re-running the reported symptom alone: the L3→L2 fold
this section asks for *already exists and already works* — it is
`abicheck/service_input_resolution.py`'s `_seeded_compile_context()`
(P0.3, wired via `derive_l2_compile_context`/`resolve_header_compile_context`
in `abicheck/buildsource/l2_seed.py` and `header_compile_context.py`), and
`resolve_side_snapshot()` (the function that calls it) is used by *both*
`service_compare_pipeline.py`'s `resolve_compare_request` (the path
`compare`'s implicit-dump side takes) **and** `service_dump_pipeline.py`'s
`run_dump_request`. The gap is narrower and more specific than "the fold
doesn't exist": **the native `abicheck dump` CLI command never calls
`run_dump_request`/builds a `DumpRequest` at all.** `abicheck/cli.py`'s
`dump_cmd` (the ELF path) calls `cli_dump_helpers.perform_elf_dump()`
directly — a separate, older code path that builds its `CompileContext`
only from explicit `--ast-frontend`/`--compiler*`/`--sysroot`/`--nostdinc`
flags and never calls `resolve_side_snapshot`/`_seeded_compile_context`/
`derive_l2_compile_context` at all (confirmed by reading
`cli_dump_helpers.py` directly: no reference to any of those three
functions anywhere in the file). This is exactly what this lab's own
audit observed: a fresh `dump --sources ... --build-info ... --depth
source` snapshot's `parsed_with_build_context: false`/
`language_standard: ""` even with real L3 evidence supplied and embedded
into the snapshot — the evidence is collected and stored, but never
routed to the L2 header-AST invocation, because that invocation runs
through a code path the L3→L2 fold was never wired into. `compare`'s own
*implicit* dump of a live binary operand does not have this gap (it goes
through `resolve_compare_request` → `resolve_side_snapshot`), which is
why the lab's own report showed a real discrepancy between `dump`-produced
baselines and `scan`'s live-binary comparison path specifically.

Upstream's own `abicheck/AGENTS.md` already names this precise migration
as deferred, independently of this lab's audit — see its "Known gaps"
entry: *"the native `dump` CLI does not build a `DumpRequest` yet — see
G33's Phase 5 note for what that migration needs first"*. This lab's
audit is the first concrete end-to-end reproduction of the consequence
(a stale/context-free `dump` baseline that a context-aware `scan` then
reports `NOT_COMPARABLE` against for a project with otherwise-complete
evidence), which is worth keeping attached to that entry rather than
treated as a newly-discovered, independent gap. Not attempted as a fix in
this lab audit pass: migrating `dump_cmd`/`perform_elf_dump` (currently
at 1914 of `cli_dump_helpers.py`'s 2000-line hard cap — real headroom is
tight) to route through `run_dump_request` is a genuine architecture
change on the upstream side, not something this lab repository can fix
from its own workflow files.

### Lab evidence

A run can have complete L3 evidence—compiler, standard, macros, include order and compile units—while the public header is still parsed independently. The report then says build context exists but also emits header-parse-context drift.

This can describe the wrong API, not merely lower confidence. Macro-conditioned declarations and include-order-dependent configuration are common examples.

### Required upstream behavior

Resolve and persist a structured header parse context:

```json
{
  "source": "build_evidence",
  "root_target": "//:math",
  "compiler": {"family": "clang", "version": "18.1.3"},
  "language_standard": "c++17",
  "target_triple": "x86_64-unknown-linux-gnu",
  "defines": [],
  "undefines": [],
  "include_paths": [],
  "forced_includes": [],
  "abi_flags": [],
  "fingerprint": "sha256:..."
}
```

Resolution rules:

1. restrict compile units to requested root targets;
2. identify units that include each public header;
3. canonicalize compile contexts;
4. parse once when contexts are equivalent;
5. build a multi-context manifest or return `NOT_COMPARABLE` when materially different contexts exist;
6. never select an arbitrary first TU;
7. compare context fingerprints before interpreting header differences.

### Acceptance criteria

Supplying complete target evidence removes generic context drift because the context was actually applied. Changes in `-D`, `-U`, include order, `-std`, sysroot, compiler, standard library, or target triple produce explicit profile/build-context outcomes instead of phantom API changes.

---

## P0.4 First-class analysis assurance contract

### Lab evidence

The lab added `scripts/check_coverage_contract.py` because `depth: source` historically meant “attempt source collection,” not “prove source analysis was complete.” A run could return `COMPATIBLE` with zero resolved targets or no relevant parsed TUs.

The external checker is useful but reconstructs some semantics from counters and prose, and can still overclaim completeness when header context drift, reduced graph confidence, or fact-set incompatibility remains.

### Required report model

Compatibility verdict, assurance status, and gate decision must be separate:

```json
{
  "compatibility_verdict": "COMPATIBLE",
  "analysis_assurance": {
    "status": "complete",
    "requested_depth": "source",
    "effective_depth": "source",
    "root_targets": {
      "expected": ["//:math"],
      "resolved": ["//:math"]
    },
    "translation_units": {
      "selected": 1,
      "parsed": 1,
      "failed": 0,
      "skipped": 0
    },
    "exports": {
      "total": 6,
      "source_linked": 3,
      "classified_internal": 3,
      "unaccounted": 0
    },
    "header_context": {"status": "applied"},
    "fact_set": {"status": "comparable"},
    "source_graph": {"status": "complete", "degraded_passes": []}
  },
  "gate": {"decision": "pass"}
}
```

Assurance states should include:

```text
complete
partial
failed
not_comparable
not_requested
```

A CI policy must be able to require `complete` independently of whether the compatibility verdict is green.

### Required Action/service outputs

At minimum:

```text
compatibility-verdict
analysis-status
policy-gate-decision
coverage-status
root-targets-resolved
compile-units-parsed
unaccounted-exports
report-path
report-digest
```

### Acceptance criteria

No external script or regex over human-readable detail is needed to enforce requested analysis depth. A green compatibility result with partial analysis is visibly and machine-readably distinct from a complete green result.

### Lab code removable afterward

- `scripts/check_coverage_contract.py` as an independent semantic gate;
- custom coverage parsing in comment/report helpers.

---

## P0.5 Safe runtime verification boundary

**Status: resolved upstream, option 1.** `--verify-runtime` and the
`abicheck.runtime_probe` module it depended on are removed outright from
`abicheck/main` (it had already been reduced to a documented,
always-no-op safety stub -- `attempted=False` unconditionally -- before
removal); the `verify-runtime` inputs on the composite Action,
`actions/check-target`, and the `check-single`/`check-project` reusable
workflows are gone with it. This lab no longer passes `verify-runtime` to
any Action invocation; `consumer_scoped` (`abi-scan.yml`) relies on
static `--used-by` alone, per "Consumer/app-scoped validation" in
`README.md`. The section below is kept as the historical record of the
lab evidence and the two options this doc originally posed.

### Lab evidence

Executing a historical consumer with analyzed shared libraries through `LD_LIBRARY_PATH` loads constructors and other load-time code from artifacts under analysis. That is unsafe in ordinary PR CI and currently requires disabling `verify-runtime`.

### Required upstream design

Choose one clear contract:

1. keep runtime verification disabled/deprecated in the normal process; or
2. provide an isolated runner with an explicit trust boundary.

A real isolated mode requires, at minimum:

- disposable container or VM;
- no repository/cloud credentials;
- no writable host workspace;
- network disabled by default;
- read-only input mounts;
- CPU/memory/time/process limits;
- syscall/filesystem restrictions;
- explicit opt-in and clear report provenance;
- unsupported-platform behavior reported as `not_available`, never silently skipped.

Static `--used-by` analysis remains the safe default.

### Acceptance criteria

A normal `abicheck` invocation cannot execute analyzed code accidentally. Enabling runtime verification either selects a documented sandbox backend or fails before execution.

---

## P0.6 Fail-closed expected-check aggregation

**Status: resolved upstream (`abicheck aggregate --manifest`/`--run-plan`),
now wired up on the lab side too.** Upstream's per-CLI-cleanup changelog
(`--expect`/`--optional`/`--report-prefix` removed) replaced the inline
flag list with `--manifest PATH` (`{"targets": [{"id", "required"}]}`) or
`--run-plan PATH` (projected from `abicheck project plan`) as the one way
to declare an expected-target set, with `--discovered-only` remaining the
explicit opt-out for "no declared set, just report what showed up" — the
`completed`/`missing_required`/`missing_advisory`/... classification this
section originally asked for is `ExpectedTargets`'s own coverage axis
(`abicheck/aggregate.py`). `abi-scan.yml`'s `aggregate` job now builds a
`--manifest` declaring `math` `required: true` whenever `scan` judged the
PR ABI-relevant, instead of always running `--discovered-only`. `strings`
is deliberately left undeclared rather than given `required: false`: an
earlier revision of this fix did mark it `required: false` and a review
round caught that the coverage axis (`required`) and the gate axis are
independent in `abicheck aggregate` — a declared-but-not-required
target's own report still folds into the overall exit code once
discovered, which would have let a genuinely BREAKING `strings_lib`
compare fail this job despite `scan_strings`'s own best-effort posture.
`--on-unexpected-target warn` on the undeclared report is what actually
achieves "surfaced, never gating" — see README.md's "Multi-library
aggregate gate" section for the up-to-date description. The rest of this
section is kept as the historical record of the lab evidence that
motivated the ask.

### Lab evidence

The lab aggregate job discovers whatever report artifacts happen to exist. A required target/profile that produces no report can therefore disappear from aggregation while the aggregate remains green.

### Required upstream behavior

Aggregation must consume a declared run plan, not only discovered files:

```yaml
checks:
  - id: math-l4-replay
    target: math
    profile: l4-replay
    required: true
  - id: math-l4-plugin
    target: math
    profile: l4-plugin
    required: false
```

For every expected cell, aggregate must classify:

```text
completed
new_target
not_applicable
missing_required
missing_advisory
failed_to_analyze
not_comparable
```

Required missing reports fail closed. Advisory missing reports remain visible. The aggregate report must include expected, completed, skipped, missing, and failed counts.

### Acceptance criteria

Deleting or failing a required per-target report makes the aggregate gate fail even when all discovered reports are green.

---

## P0.7 Profile-specific baselines and comparability

### Lab evidence

The lab runs several materially different profiles but stores one baseline per library. That mixes product changes with compiler, language standard, AST frontend, source-fact producer, fact-set version, and build flags.

### Required upstream model

A baseline identity is:

```text
product state × platform × compile profile × evidence profile × contract profile
```

A profile fingerprint must include at least:

- compiler family/version/executable identity;
- target triple and data model;
- C/C++ standard;
- standard library;
- defines/undefines;
- ordered include paths and forced includes;
- ABI-affecting flags;
- AST frontend;
- source-fact producer and fact-set version;
- requested root targets;
- public-header/contract roots;
- requested/effective depth.

Comparisons across incompatible profiles should return `NOT_COMPARABLE`, not convert scanner/profile drift into compatibility findings.

Recommended storage shape:

```text
baselines/
  math/
    gcc17-cxx17-castxml-l2/
    clang18-cxx17-clang-l2/
    clang18-cxx17-replay-l4/
    clang18-cxx17-plugin-l4/
```

Cross-profile analysis belongs to producer conformance, not product compatibility.

### Acceptance criteria

Every compatibility comparison uses the same profile on both sides. Profile mismatches are explicit and cannot silently pass or produce ordinary ABI break findings.

---

# P1: productization of proven lab integrations

## P1.1 Bazel declared-output rules/aspect

The lab’s `tools/abicheck/facts.bzl` solves a real Bazel correctness issue: plugin facts written as an undeclared compile side effect disappear on cache hits and are invisible to sandbox/remote execution.

Upstream a supported Bazel integration—either under `contrib/bazel` or as `rules_abicheck`—with:

- a facts aspect over C/C++ targets;
- declared tree-artifact output per TU;
- output groups and a merged facts pack;
- target/dependency propagation;
- generated-header support;
- exact compile variables from `cc_common`;
- correct handling of public roots carried as `-isystem`;
- compiler/plugin identity in the action key;
- local/remote cache and sandbox tests;
- no undeclared filesystem writes.

Normal user flow should become approximately:

```text
bazel build //tools/abicheck:math_facts
abicheck scan --build-info bazel-bin/tools/abicheck/math_facts ...
```

Lab code removable: `tools/abicheck/facts.bzl`, its wrapper BUILD targets, and custom plugin-facts collection workflow steps.

---

## P1.2 First-class producer conformance command

The lab has two useful comparisons that are not ordinary ABI comparisons:

- CastXML versus direct Clang;
- replay versus compiler plugin.

Add a command such as:

```text
abicheck conformance compare left.json right.json -o conformance.json
```

Match by `canonical_finding_id` and structured evidence identity, not raw prose. Classify:

```text
semantic_agreement
textual_spelling_difference
coverage_asymmetry
producer_only_finding
fact_set_mismatch
probable_producer_bug
not_comparable
```

Conformance must never masquerade as the library’s compatibility verdict. It is a producer-quality result with its own status and CI policy.

Lab code removable: `scripts/render_conformance_report.py` and duplicated matching rules.

---

## P1.3 Per-check profile axes in `.abicheck.yml` and run plans

The reusable project workflow must represent each target/profile cell without hand-written jobs. Per-check fields need to include:

- target and build targets;
- compile profile/toolchain binding;
- requested depth;
- AST frontend;
- source-fact producer;
- contract mode;
- policy/suppressions;
- required versus advisory;
- baseline channel/profile/generation;
- gate mode (`local`, `deferred`, `advisory`).

The lab should ultimately express its matrix declaratively and invoke one reusable `check-project` workflow.

---

## P1.4 Canonical baseline serialization

The lab’s schema-unaware baseline normalization once removed a required field. Upstream a canonical serializer instead of requiring JSON surgery:

```text
abicheck dump --canonical \
  --provenance-output run-provenance.json \
  -o baseline.abicheck.json
```

Semantic baseline should retain ABI/API facts, profile and contract fingerprints, producer/fact-set identity, normalized source locations, assurance summary, schema version, and semantic digest.

Move timestamps, runner IDs, temporary paths, elapsed time, cache metrics, execroot paths, and host-specific details to a provenance sidecar.

Acceptance criterion: two equivalent extractions produce the same semantic digest without external normalization.

---

## P1.5 Trusted baseline retrieval from the exact PR base

The lab correctly reads a committed baseline with:

```text
git show <pull_request.base.sha>:<baseline-path>
```

so a PR cannot edit both the ABI and its own baseline and pass against the modified file.

Upstream reusable workflows should provide this as a normal baseline source, including:

- exact base SHA/ref in the report;
- file/artifact digest verification;
- explicit failure when the trusted baseline cannot be resolved;
- fork-safe/read-only behavior;
- compatibility with accepted-main and release-contract channels.

---

## P1.6 Target-aware changed-path relevance

The lab avoids workflow-level `paths:` filters because a required workflow that never triggers remains pending. It always starts, computes relevance inside the job, uses `git diff --no-renames`, and narrows build-file relevance through Bazel’s actual load graph.

Upstream reusable workflows should own this behavior and return a structured skip result while still completing the required check.

Required cases:

- relevant file renamed to an irrelevant path;
- generated/public header changes;
- BUILD/.bzl files inside and outside the target’s load graph;
- target addition/removal;
- source/config changes that affect one target but not another.

---

## P1.7 Historical consumer-scoped compatibility recipe

The lab correctly builds the already-deployed consumer and old library from the PR base SHA. Rebuilding the consumer from PR head can hide a real break by linking it against the new mangled symbol.

Add a reusable Action/workflow recipe for:

```text
old consumer + old library from trusted base
candidate library from PR head
static --used-by analysis
optional isolated runtime verification
```

The report should state whether the consumer is historical or a bootstrap fallback. A HEAD-built fallback must never be presented as equivalent evidence.

---

## P1.8 Stable Action and service outputs

The lab currently reads reports and step outcomes in custom scripts. Expose stable outputs for orchestration:

```text
compatibility-verdict
analysis-status
gate-decision
baseline-outcome
not-comparable-reason
report-path
report-digest
profile-fingerprint
scope-fingerprint
root-targets-resolved
suppressed-count
```

All front ends—CLI, Action, reusable workflows, Python API and service API—must normalize into one evaluation request and emit equivalent semantics.

---

# P2: validation, maintenance, and observability

## P2.1 Manifest-driven end-to-end scenario oracle

Upstream a compact scenario runner based on the lab’s manifest. An oracle should assert more than the top-level verdict:

- required/forbidden finding kinds;
- canonical finding IDs or stable symbol identities;
- suppressed identities and remaining gating identities;
- assurance status;
- root targets and TU counts;
- profile/comparability outcome;
- unexpected findings;
- report/schema validity.

Add source-level scenarios for macro changes, inline/template body changes, private implementation changes, generated headers, unrelated Bazel targets, parse failures, graph degradation, fact-set mismatch, missing required reports, new targets, and stale suppressions.

The scenario suite belongs in `abicheck` tests/examples; the lab remains an external canary.

---

## P2.2 Scheduled fresh-main and stale-suppression canary

The lab’s scheduled scenario canary tests the pinned consumer integration against current `abicheck/main`. Productize the general pattern:

- re-run saved oracles against a candidate scanner;
- identify verdict drift separately from infrastructure failure;
- audit suppression rules that matched zero findings;
- classify desired detector improvement versus probable regression;
- produce a scanner-upgrade report before pins/baseline generations move.

---

## P2.3 Performance and cache benchmarks

The lab measures cold/warm Bazel and `abicheck` behavior. Upstream benchmarks should record:

- exact scanner/toolchain SHA;
- repeated median and p95;
- variance;
- wall time and peak RSS;
- Bazel query/build time;
- L2 extraction time;
- L4 replay/plugin time;
- compare/report time;
- pack/snapshot size;
- selected/parsed TUs;
- cache hit ratio;
- replay versus plugin.

Use explicit regression thresholds and retain machine-readable history.

---

## P2.4 Compiler, standard, frontend, and producer matrix

Turn the lab matrix into a reusable profile matrix rather than independent hand-written jobs. Each report must carry the resolved profile and compare only against its matching baseline.

Minimum axes demonstrated by the lab:

- GCC versus Clang;
- C++17 versus C++20;
- CastXML versus direct Clang;
- replay versus plugin;
- Intel LLVM/ICX experiments where supported.

The matrix should distinguish compatibility failures from expected profile divergence and producer-conformance failures.

---

## P2.5 Repository and baseline-chain integrity

The lab also demonstrates that the checker is only as trustworthy as its CI chain. Provide guidance/tests for:

- protected default branch;
- CODEOWNERS/rulesets for baselines, policies, suppressions and workflows;
- minimal token permissions;
- immutable exact Action pins;
- baseline digest/attestation;
- trusted-base retrieval;
- no PR-controlled baseline self-approval;
- no untrusted artifact execution;
- report provenance and scanner SHA.

---

# Lab-to-product deletion map

Once the corresponding upstream work lands and is validated, the following lab components should shrink or disappear:

| Lab component | Upstream replacement |
|---|---|
| Manual Bazel `cquery` / `aquery` steps | Typed root-target-scoped Bazel collection |
| `scripts/build_bazel_evidence_pack.py` | Built-in Bazel adapter/pack creation |
| `scripts/check_coverage_contract.py` | `analysis_assurance` plus assurance gate |
| `tools/abicheck/facts.bzl` | Supported `rules_abicheck`/contrib Bazel aspect |
| `scripts/render_conformance_report.py` | `abicheck conformance compare` |
| `scripts/render_scan_comment.py` | Native scan PR-comment renderer |
| Custom sticky-comment GitHub Script | Native Action comment support |
| Baseline JSON normalization script | Canonical serializer + provenance sidecar |
| Discovered-only aggregate wiring | Run-plan-aware fail-closed aggregate |
| Hand-written target/profile jobs | Declarative `.abicheck.yml` + `check-project` |
| Historical-consumer worktree shell | Reusable consumer-scoped workflow |
| Custom performance summary logic | Upstream benchmark schema/renderer |

The lab should retain only small fixtures, profile declarations, expected outcomes, and external canary workflows.

---

# Recommended implementation order

## Wave 1: correctness and security

1. Cross-producer constructor/destructor matching.
2. Explicit Bazel root-target scoping.
3. Apply L3 build context to L2 parsing.
4. Structured assurance contract and gate.
5. Safe runtime-verification boundary.
6. Expected-check fail-closed aggregation.
7. Profile-specific baseline comparability.

## Wave 2: product integration

8. Per-check profile axes in configuration/run plans.
9. Supported Bazel declared-output aspect/rules.
10. Canonical baseline serializer.
11. Trusted-base and historical-consumer reusable workflows.
12. Stable Action/service outputs.
13. First-class producer conformance.

## Wave 3: validation and maintenance

14. Upstream scenario oracle.
15. Scanner-upgrade/stale-suppression canary.
16. Performance regression suite.
17. Reusable compiler/frontend/producer matrix.
18. CI/baseline integrity guidance and checks.

---

# Definition of done

The lab can be considered a thin, representative integration only when all of the following hold:

1. An unchanged library produces semantically equivalent results in CastXML, direct-Clang, replay, and plugin profiles.
2. No false synthetic-constructor removal remains.
3. Requested Bazel roots—not `//...`—determine targets, TUs, graph, and coverage.
4. Complete build evidence is actually applied to public-header parsing.
5. Compatibility verdict, analysis assurance, and policy gate are separate structured results.
6. Missing required per-target/profile reports fail aggregate CI.
7. New targets produce `NEW_TARGET`, not an unreported skip.
8. Analyzed binaries are never executed in ordinary PR CI.
9. Baselines are profile-specific and cross-profile pairs are `NOT_COMPARABLE`.
10. Producer comparisons use a conformance result, not a library compatibility verdict.
11. Baseline serialization is deterministic without external JSON rewriting.
12. A trusted baseline is resolved from immutable base/release state.
13. Historical consumer analysis uses the deployed consumer, not a PR-head rebuild.
14. The normal user workflow reduces to: build, run configured checks, aggregate, publish.
15. The lab no longer owns duplicate implementations of product semantics.

---

# 2026-08-21 follow-up: no first-class build-system/generator identity in `build-output.json`

**Context:** PR1 of a new, separate multi-build-system integration effort
(`ci/profiles.yaml`, `ci/backends/{bazel,cmake,make}.py`,
`buildsystems/{cmake,make}/`, `ci/emit_build_output.py`) adds a second and
third build system -- CMake+Ninja and a handwritten Makefile -- alongside
the existing, still-sole-required Bazel build, each staging a canonical
`abicheck-build-<profile-id>/build-output.json` per profile (see
`ci/schemas/build-output.schema.json` and README.md's "ABICheck
Integration Lab: multi-build-system profiles" section). Neither new build
system is wired to an ABICheck scan yet (that is later-phase scope, not
this PR's) -- this entry documents a gap the *staging* work already
surfaced, independent of when scanning lands.

**Gap:** nothing in abicheck's own report/dump/build-evidence schema has a
first-class field for "which build system, and which generator, produced
this build evidence" (P0.7's own profile-fingerprint list above --
compiler family/version, standard, target triple, etc. -- has no
`build_system`/`generator` entry either, and P0.2's `build_system: bazel`
request shape is scoped to Bazel-target-resolution specifically, not a
general build-system-identity field every evidence-producing profile
would populate). A `BuildEvidence`/`BuildSourcePack`-shaped object
produced from a CMake+Ninja or Make build today has no standard place to
record "this evidence came from CMake, generator Ninja" versus "this
evidence came from a handwritten Makefile" versus Bazel's own
`bazel cquery`/`bazel aquery` shape -- a consumer has to infer it
out-of-band (a profile-id naming convention, a sidecar file) rather than
reading it off the evidence object itself.

**Lab-side workaround, current state:** this PR encodes the missing
identity two ways, neither of which is a report-schema field abicheck
itself understands:

1. **`profile.id`** (`ci/profiles.yaml`, e.g.
   `linux-x86_64-gcc14-cxx17-cmake-ninja`) -- a naming convention, not a
   structured field; a consumer has to parse the id string to recover
   `backend=cmake`/`generator=Ninja`.
2. **`provenance/build-system.json`** (written per staged profile by
   `ci/emit_build_output.py`, from each `BuildBackend.describe()`) -- a
   real structured object (`{"backend": "cmake", "generator": "Ninja",
   "compiler": {...}, "cmake_version": "...", "cxx_version": "..."}` for
   the CMake profile; the equivalent shape for `bazel`/`make`), but it
   lives as a sidecar file next to `build-output.json`, not as a field
   `build-output.json`'s own `schema_version: 1` shape declares --
   `ci/schemas/build-output.schema.json`'s own top-level docstring already
   flags this as provisional for exactly this reason.

**What upstream should provide instead (aligned with P0.7 above, not a new
ask):** extend the profile-fingerprint model P0.7 already specifies with
an explicit `build_system: {name, generator}` field (`name` one of at
least `bazel`/`cmake`/`make`/`msbuild`/..., `generator` populated only
where the build system has one -- e.g. CMake's `Ninja`/`Unix Makefiles`,
null for Bazel/Make, which don't have a separate generator concept), so
`BuildEvidence`/`BuildSourcePack`/a report's own build-context object
carries build-system identity as a first-class, machine-readable field
across every producer, the same way P0.7 already asks for compiler
family/version/standard identity. Once that lands, this lab's own
`profile.id`-encodes-it-in-the-string-plus-sidecar-file workaround
(`provenance/build-system.json`) becomes redundant and should be replaced
by reading the field directly out of whatever evidence object a future
PR's ABICheck-scanning phase actually feeds into `abicheck scan`/`compare`
for the CMake/Make profiles.

---

# 2026-08-21 follow-up: PR2's ABI check is a symbol-table diff, not the real scanner -- because the real scanner is unreachable, not by choice

**2026-08-21 update: resolved for cmake/make, not a general pattern to
follow for bazel's own advisory leg.** A later session's own sandbox DID
have network access (`pip download`/`pip install` against the exact pinned
`abicheck @ git+https://github.com/abicheck/abicheck.git@6fb85361cf4cea67
a2f444bc097cfe24cd2d99c3` URL both succeeded), confirming this entry's own
"a real CI run does have network access and could install the real
scanner" prediction. `ci/real_scan.py` now runs a real `abicheck dump
--depth source`/`compare` invocation for the cmake and make profiles,
verified end-to-end against this repo's own real CMake+Ninja build: a
self-comparison reports `NO_CHANGE`, and a real function removal from
`src/math.cc`/`include/abicheck_lab/math.h` reproducibly reports
`BREAKING` via `abicheck compare` -- not the symbol-table mechanism this
entry originally documented. `ci/check_profile.py`'s nm/readelf mechanism
(the rest of this entry, kept as the historical record of what it is and
is not) remains in place only for the Bazel profile's own leg of this same
advisory workflow -- Bazel already has its own real, required gate in
`abi-scan.yml`, so its leg here never needed the real-scanner path this
update describes. One integration detail worth recording for anyone
repeating this: both CMake's and Make's own `compile_commands.json`
describe the whole project (all of `math`/`strings`/`consumer` in one
database), and handing the unfiltered database to `abicheck dump -H
<target's header dir>` fails closed with a real, correct error -- the
consumer's own TU also includes the target's public header under a
materially different compile context (`-fPIE` vs the library's own
`-fPIC`), so abicheck refuses to guess which context to parse the header
under, exactly analogous to this doc's own P0.2 entry for Bazel's
`deps(//...)` scope. `ci/real_scan.py`'s `filter_compile_db_for_target`
pre-filters the database to the target's own translation unit before it
reaches `abicheck dump`, the same way Bazel's own target-scoped
`cquery`/`aquery` already does for that profile.

**Context:** PR2 of the multi-build-system integration effort wires an
actual ABI comparison into all three `ci/profiles.yaml` profiles
(`ci/check_profile.py`, per-profile receipts via
`ci/emit_profile_receipt.py`, a profile-aware coverage contract via
`ci/check_profile_coverage.py`, and an advisory `integration_gate` job in
`.github/workflows/integration-shadow.yml`), still entirely non-gating
(`.github/workflows/abi-scan.yml`'s own required Bazel gate is untouched).

**Gap:** the real `abicheck` scanner this lab otherwise depends on
everywhere else (`abi-scan.yml`'s `pip install abicheck @ git+https://
github.com/abicheck/abicheck.git@...`) requires network access to install,
and this PR's own development/validation sandbox has none (verified, not
assumed: `pip download` against that exact git+https URL times out with no
response, and `python3 -c "import abicheck"` fails -- nothing under
`tools/abicheck/` is a vendored copy of the scanner itself, only Bazel glue
that *feeds* the external CLI a source-fact pack). A real CI run of this
repo's own GitHub Actions runners does have network access and could
install the real scanner, but PR2's own local validation could not depend
on that, and the task that produced it was explicit: do not fake or stub
the real scanner's behavior.

**What PR2 does instead, honestly:** `ci/check_profile.py` diffs each
staged shared library's **dynamic symbol table** (`nm -D`/`readelf`,
present on every runner already, no install needed) between a committed,
per-profile baseline and the freshly built candidate, plus a content-hash
diff of the target's staged public headers. This is a real, source-of-truth
ABI compatibility signal -- not a mock -- verified end-to-end against real
compiled output for all three profiles (Bazel, CMake+Ninja, Make), on both
the happy path (identical source -> `NO_CHANGE` against every profile's own
baseline) and a real regression (a function removed from `src/math.cc`/
`include/abicheck_lab/math.h` -> `BREAKING`, symbol correctly named in the
report). It compares exported symbols added/removed, a data symbol's ELF
type/size (a code symbol's compiled size is deliberately ignored -- it
legitimately changes on every ordinary recompile and isn't ABI-relevant on
its own), and the library's SONAME (or, when no SONAME exists at all --
Bazel's `cc_shared_library` outputs -- its on-disk loader filename
instead); any of those changing is `BREAKING`. It cannot catch anything
invisible at the symbol-table/SONAME level -- a struct/class layout change
with no symbol rename, a default-argument change, an inline-body change, a
template instantiation change. `ci/check_profile.py`'s own module
docstring documents this in full; `public_headers_changed` in every report
is the honest "something in the API surface changed that this mechanism
cannot itself classify" signal, always surfaced, never silently dropped --
when a header changed and nothing else did, the verdict is `NOT_COMPARABLE`
(not `COMPATIBLE`), reported as unclassified rather than as a proven
compatible change.

**What upstream/this lab should do once a real scanner is reachable from
this integration's own CI/dev environment (a later PR, not blocked on
anything upstream -- this is purely an environment gap, not a product
gap):** replace `ci/check_profile.py`'s nm/readelf mechanism with a real
`abicheck compare`/`scan` invocation for the CMake/Make profiles too (the
same P0.2/P0.7 profile-fingerprint and root-target-scoping asks already
listed above apply directly -- CMake's `compile_commands.json` and Make's
`bear`-generated equivalent are both legitimate `--build-info`-shaped
inputs once abicheck accepts a non-Bazel build-evidence source), and retire
the symbol-table-only baselines under `abi/profiles/<id>/` in favor of real
`abicheck dump` snapshots, the same way `abi/math.abicheck.json` already
is for Bazel. `ci/check_profile.py`'s own `dump`/`check` CLI shape (and
`ci/emit_profile_receipt.py`/`ci/render_integration_gate.py`'s receipt/gate
plumbing around it) should stay useful as the integration seam either way --
only the comparison mechanism inside `check_profile.py` needs replacing,
not the profile/receipt/gate architecture built around it.

---

# 2026-08-21 follow-up: no cross-build-system public-contract equivalence primitive upstream

**Context:** PR3 of the multi-build-system integration effort adds
`ci/compare_build_outputs.py` -- a pairwise (Bazel↔CMake, Bazel↔Make,
CMake↔Make) comparison of two profiles' own staged build output for the
same commit, answering a question neither PR2's per-profile baseline check
nor abicheck itself can: does Bazel's build of a library mean the same
public ABI as CMake's or Make's build of the identical source. Still
entirely non-gating (`cross_build_equivalence` in
`.github/workflows/integration-shadow.yml`; `abi-scan.yml`'s own required
gate is untouched).

**Gap:** abicheck has no built-in notion of "compare two build systems'
own outputs of the same commit" at all -- `abicheck compare` (and every
other command) takes two *candidate* artifacts (old/new, a version bump,
a release), never two *build systems'* independent productions of the
*same* version. This is design doc section 29's own expected gap #8
("Cross build public-contract equivalence reporting"), now confirmed with
a real implementation and a real finding, not just anticipated: run for
real against this repo's own three profiles (gcc-14, Bazel 7.4.1,
CMake+Ninja, Make+Bear -- all locally provisioned this session, unlike the
PR2 entry above, this session's own sandbox DID have outbound network
access, confirmed by successfully installing the real `abicheck` package
from its git+https source), the comparison surfaced a genuine
`public_contract_mismatch`: Bazel's `//:math` (the `cc_binary(linkshared
= True)` demo target shape) sets **no SONAME**, CMake's build sets
`libmath.so.1`, and Make's build sets `libmath.so` -- three real, different
answers for the identical source, exactly the class of finding this
comparison exists to catch, and exactly the class of finding no existing
abicheck command could have surfaced (each side's own `abicheck dump`/
`compare` against its own baseline reports `NO_CHANGE`; SONAME is only
visible by directly comparing the two sides' own artifacts against each
other, which nothing in abicheck's own command surface does).

**Lab-side workaround, current state:** `ci/compare_build_outputs.py` is
entirely bespoke lab tooling -- it reuses `ci/check_profile.py`'s own
`nm -D`/`readelf` extraction functions (dynamic symbol table, SONAME,
public-header digest) directly rather than through any abicheck API, adds
its own `NEEDED`-dependency extraction, and layers a five-way
classification taxonomy (`public_contract_mismatch`/
`toolchain_configuration_mismatch`/`expected_build_system_metadata_
difference`/`evidence_only_difference`/`unclassified_difference`) on top
so a real ABI divergence doesn't get lost among build-system bookkeeping
that's expected to differ (target ids, generator, evidence-producer
paths). When the real `abicheck` CLI is reachable, it also opportunistically
runs one real `abicheck compare` directly between the two profiles' own
built libraries (old="profile A", new="profile B") as a deeper,
best-effort signal beyond the symbol-table diff -- but this is a
one-off subprocess call this script owns and interprets itself, not a
first-class abicheck comparison mode.

**What upstream should provide instead:** a first-class `abicheck
compare-builds` (or an existing-command mode) that takes N build-output
directories (or N pairs of `--binary`/`--header` inputs) already known to
represent the SAME logical version/commit and reports build-system-to-
build-system equivalence directly, with its own classification of
"expected build metadata difference" versus "real public-contract
divergence" -- built on the same DWARF/header-AST evidence `compare`
already collects, rather than a lab-side nm/readelf reimplementation that
can only ever see what's visible at the ELF symbol-table level (this
lab's own `abicheck_cross_check` best-effort step is exactly the stopgap
such a command would replace). Once available, `ci/compare_build_outputs.py`
should become a thin driver over that command (resolving which staged
profiles to compare, classifying/rendering the result for this repo's own
`cross_build_equivalence` job), not a parallel comparison engine.

---

# 2026-08-21 P0: `abicheck scan --against` reports NOT_COMPARABLE against every `abicheck dump` baseline, deterministically, for reasons unrelated to the library

**Severity: P0.** This is not a lab-only workaround entry -- it is the
required, load-bearing `abi-scan.yml` `scan` job itself, failing on every
single PR against `main`, confirmed on four independent CI runs (including
a bare re-run with zero code changes) and reproduced cleanly, offline, in
this session's own sandbox, isolated all the way down to a single root
cause inside `abicheck` itself.

**Symptom (real CI, `abicheck/integration-lab` PR #23, and every PR before
it since the `6fb8536` pin landed):**

```
abicheck verdict: NOT_COMPARABLE (exit code 6)
scan --against reported NOT_COMPARABLE: the candidate and baseline were not
extracted under a comparable profile/scope contract.
diff.reason: "old and new snapshots were extracted under different compile
contexts (profile_fingerprint mismatch; differing fields: include_sequence)"
```

**Root cause, isolated by direct reproduction (not inference):**

1. `baseline.yml`'s "Collect source-aware ABI baseline" step calls
   `abicheck/abicheck@6fb8536` with `mode: dump`. `abi-scan.yml`'s "ABICheck
   source scan" step calls the *same pinned Action commit* with `mode: scan`
   (`--against <the dump baseline>`).
2. Verified `scripts/build_bazel_evidence_pack.py`'s own output (the
   `--build-info` evidence pack both modes consume) is **byte-identical**
   across two entirely separate `bazel build //:math` + `cquery`/`aquery`
   invocations from the identical commit (`sha256sum` match on both
   `manifest.json` and `build/build_evidence.json`) -- ruling out Bazel/this
   repo's own evidence extraction as the source of non-determinism.
3. Verified two separate `abicheck dump` invocations against two separately
   *built* (but content-identical) evidence packs produce structurally
   identical `contract.profile_fingerprint`/`include_sequence` values
   (only wall-clock timing fields differ) -- ruling out `dump` mode itself,
   and ruling out pack-to-pack determinism, as the source.
4. **The isolating test:** ran `abicheck scan --against <a dump.json>`
   using the *exact same evidence pack* that `dump.json` was itself
   produced from (same binary, same headers, same sources, same toolchain,
   same pack -- nothing held constant except the mode). Result:
   `NOT_COMPARABLE`, `differing fields: include_sequence`, every time.

**Conclusion:** `abicheck scan`'s own internal computation of the
`include_sequence` profile-fingerprint field for its candidate ("new")
side does not agree with `abicheck dump`'s computation of the identical
field, for byte-identical underlying evidence. Since `comparability.py`'s
profile-fingerprint gate (correctly, by design) refuses to compare two
snapshots whose extraction context doesn't match, and a `dump`-produced
baseline compared via `scan --against` can apparently *never* match on
this specific field, this makes `scan --against <a dump-mode baseline>`
structurally unable to succeed -- not a flake, not environment drift, not
anything about the library's own source or ABI.

**What this means for every repo using this exact pattern** (dump-mode
baselines + scan-mode PR gate, both via `abicheck/abicheck@6fb8536` or
any release sharing this bug): the required gate cannot produce a real
`COMPATIBLE`/`BREAKING`/`NO_CHANGE` verdict at all right now -- every PR
either hits `NOT_COMPARABLE` (if `fail-on-breaking`-style gating treats it
as a failure, as `abi-scan.yml`'s own "Enforce gate" step does) or would
need to treat `NOT_COMPARABLE` as non-blocking (silently defeating the
gate's whole purpose).

**What this lab cannot do:** patch `abicheck` itself -- this bug is inside
the pinned Action commit's own `comparability_fields.py`/`comparability.py`
(the `include_sequence` computation path taken by `mode: scan`'s own
candidate-snapshot construction), not in anything this lab repo owns.
Refreshing `abi/math.abicheck.json` (confirmed: triggered a manual
`baseline.yml` re-run, produced byte-identical content, landed no new
commit) does not and cannot fix this -- the mismatch is structural between
the two *modes*, not about baseline staleness.

**What should happen upstream:** `abicheck scan`'s own new-side extraction
path should compute `include_sequence` (and the rest of
`profile_fields`) through the *same* code path `abicheck dump` uses for
identical inputs -- or, if the two paths are intentionally different
implementations, the profile-fingerprint comparability gate needs a
`scan`-vs-`dump`-mode-aware normalization/waiver so a `dump`-produced
baseline remains usable as a `scan --against` target at all. Right now
neither holds, and the gate cannot pass.

**What this repo should consider in the meantime** (not implemented by
this entry -- a decision for whoever owns `abi-scan.yml`/`baseline.yml`,
since it changes the required gate's own trusted mechanism): either (a)
generate the committed baseline via `mode: scan` against a null/self
comparison instead of `mode: dump`, if the CLI supports producing a
scan-compatible snapshot that way, or (b) pin to an `abicheck` commit
predating whatever change introduced this `scan`-vs-`dump` fingerprint
divergence, until it's fixed upstream, or (c) report this exact
reproduction to `abicheck/abicheck` and pin to the fix once released.
