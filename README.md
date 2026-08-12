# ABICheck Bazel Lab

A small public C++ shared-library project used to validate ABICheck GitHub Actions.

- `baseline.yml` builds `//:math` on `main`, collects a source-aware ABI
  snapshot, normalizes it (see below), and commits it to
  `abi/math.abicheck.json` if the normalized content changed.
- `abi-scan.yml` runs on pull requests, builds the candidate with Bazel,
  resolves the trusted baseline from the PR's exact base commit, and runs a
  **single** ABICheck source scan that is simultaneously the merge gate,
  the source of the PR comment, and the job summary (`scan` job). A second,
  non-blocking `canary` job runs the same scan pinned to a fixed
  `abicheck/main` commit instead of the last release, so a PR shows what the
  next abicheck release will do before it ships.

Test PRs intentionally exercise compatible additions, binary ABI breaks,
source/API breaks, and implementation-only changes.

## Gate / PR-comment architecture

The `scan` job runs exactly one gating `abicheck` invocation (`mode: scan`,
`depth: source`, `format: json`, `abicheck-report.json`). Its outcome drives
all of the following, so there is one verdict and one report, not two
analyses that can disagree:

- pass/fail gate (`fail-on-breaking`, `fail-on-api-break`)
- the PR comment (see below)
- the job summary (abicheck's native `add-job-summary`, which does support
  `scan` mode)
- the `abicheck-report` artifact, uploaded with `if: always()` so it is
  available even when the gate fails

The baseline it compares against is read directly out of git history at the
PR's exact base SHA (`git show <base-sha>:abi/math.abicheck.json`), not from
the working tree. A PR that edits `abi/math.abicheck.json` in the same diff
that breaks the ABI cannot pass by comparing itself against its own edited
baseline — the trusted side of the comparison always comes from `main` as it
stood before the PR branched. `abi/**` also has a `CODEOWNERS` entry, so a
baseline edit that does land on `main` still needs review, closing the gap
for a change that only touches `abi/math.abicheck.json` outside a PR that
also changes the library (once branch protection requires code-owner review
on `main` — this repo can declare that requirement, but can't turn it on for
itself).

`since` is likewise pinned to `github.event.pull_request.base.sha` rather
than a moving branch ref, so the analysis is reproducible even if `main`
advances while the PR's checks are running.

### Why the PR comment isn't abicheck's own `pr-comment`

abicheck's built-in sticky PR-comment renderer (`pr-comment: true`) only
activates for `mode: compare` — in both the pinned v0.5.0 release and
current `abicheck/main`, `_maybe_post_pr_comment` in `action/run.sh` is a
silent no-op for `mode: scan`. That's exactly how the *previous* version of
this workflow ended up rendering its PR comment from an unrelated `compare`
call with none of the scan's `sources`/`depth`/`since`/build context — the
comment reported "no L3–L5 evidence" for an analysis that never collected
any, while a *different* analysis (the one with real source evidence) was
actually gating the PR.

Since scan-mode PR comments don't exist upstream yet, `scripts/render_scan_comment.py`
renders a small, literal comment directly from the same `abicheck-report.json`
that gates the PR (verdict, exit code, requested vs. effective depth, and
the evidence-gap `advisories` list abicheck itself emits — the same list
that says things like "Macros, default args, inline/template/constexpr
bodies are off; source-only API changes are not detected"). It's posted/
updated as a sticky comment via `actions/github-script`. This is a
same-repo workaround, not a fix: the real fix is a `scan`-mode PR-comment
renderer upstream in abicheck.

A `compare`-mode step still runs for a detailed binary/header-only diff
(useful for humans who want a full side-by-side), but it's explicitly
non-gating (`fail-on-breaking`/`fail-on-api-break: false`) and its JSON is
published **only** as the `abicheck-diagnostic-compare` artifact — not as a
second PR comment — so it can't be mistaken for the canonical report again.

### Coverage gap this surfaces (not yet fixed)

Even with the architecture above, the underlying evidence gap the review
identified is real and still open: a `depth: source` request over this
lab's Bazel target frequently can't actually achieve source-level evidence
(0/6 exported symbols linked to source, 0 Bazel targets resolved), and
abicheck v0.5.0 still reports `COMPATIBLE` rather than an incomplete-coverage
verdict when that happens — because the coverage/contract axis
(`--contract-evaluation`, ADR-049) that would produce `COVERAGE_INCOMPLETE`
doesn't exist in v0.5.0 at all. The `canary` job runs the same scan against
`abicheck/main` with `extra-args: '--contract-evaluation'` specifically to
surface this: it's a preview of the fix, not a fix in this repo, and it
never blocks the PR (no PR comment, job-summary only, `fail-on-*: false`).
The Bazel-integration work the underlying 0/6-symbols gap actually needs
(mapping `bazel-bin/libmath.so` back to `//:math`, real compile actions via
`aquery`/`CcInfo`, public-header provenance) lives in the `abicheck` tool,
not this repo.

## Baseline determinism

Raw `abicheck --mode dump` output embeds run-specific noise: wall-clock
timestamps, absolute runner paths (`/home/runner/work/...`,
`/home/runner/.cache/bazel/...`), filesystem mtimes, per-run extractor
timings, and content-hash fields computed over that volatile payload before
this script ever sees it. Left in place, every baseline refresh would
produce a commit even when the public ABI didn't change.

`scripts/normalize_baseline.py` runs between `dump` and the commit step.
It deletes an **explicit allowlist of exact paths** (`DELETE_PATHS`), each
verified against the real committed `abi/math.abicheck.json` before being
added — not a key-name pattern applied broadly. Two earlier attempts at a
broader pattern both turned out to delete real ABI content: a name-keyed
map's *key* is itself arbitrary content (a real constant or function could
legitimately be called `timeout_s` or `elapsed`; `build_source`'s own
source-linkage evidence — `mappings`, `source_edges`, `indexes.by_target`/
`by_file`/..., preprocessor `defines` — is keyed by real file paths, symbol
names, and target labels, not just provenance), so no subtree of the
document is safe to filter by key-name pattern. The exact paths currently
stripped: the document-root `created_at`/`version`/`git_commit` (the
latter two because `baseline.yml` passes `new-version:
main-${{ github.sha }}`, which would otherwise change on every push
regardless of whether the ABI did — git history on the file already
records which commit each refresh reflects), `build_source_pack
.content_hash`, and inside `build_source`: `manifest.created_at`,
`manifest.artifacts`, each extractor's `artifacts`, each coverage entry's
`elapsed_s`, `source_abi.coverage`'s `cache_lookup_s`/`extract_s`/
`link_s`/`elapsed_s`, and `source_graph.graph_id`. A future abicheck
version adding a new volatile field elsewhere shows up as a small amount
of visible, debuggable baseline noise rather than a silently deleted ABI
entity — that asymmetry is deliberate.

Absolute-path rewriting (repo checkout → repo-relative,
Bazel execroot/output → `bazel-out/...`-relative) is scoped the same way:
only `source_location`/`source_header`/`source_path`, fixed schema field
names of function/type descriptors that are never the key of a
name-keyed content map, so matching them by name can't collide with real
content the way `DELETE_PATHS`-style stripping could. No other absolute
paths that vary run-to-run have been found anywhere else in the tree; the
only other absolute paths present (`compile_units[].argv[0]`,
`link_units[].linker_argv[0]`, e.g. `/usr/bin/gcc`) are stable toolchain
paths, not noise, and are left untouched. `<N>s` timing fragments in the
extractor `detail` field (also a fixed schema field name) are collapsed to
a constant placeholder.

Verified idempotent (double-normalizing is a no-op) against the committed
baseline, and against a synthetic report exercising every collision this
design guards against (a `constants.timeout_s` entry, a function named
`elapsed_s`, a `source_graph.indexes.by_target` key containing `timeout_s`,
a `defines` map with `timeout_s`/`elapsed`, and path-like constant values)
— all survive `normalize()` unchanged while the real volatile fields
listed above are still stripped.

## Build environment

`.bazelversion` and `.bazelrc` pin the Bazel version and an explicit
`-std=c++17`, so a compiler/standard bump can't silently change name
mangling/ABI without touching anything this repo's own path filters watch.
Both `abi-scan.yml` and `baseline.yml` cache Bazel's disk cache
(`~/.cache/bazel-disk`) via `actions/cache`, keyed on
`MODULE.bazel`/`.bazelversion`/`.bazelrc`/`BUILD.bazel`.

**Not yet added:** a `MODULE.bazel.lock`. Generating one correctly needs a
real `bazel mod deps --lockfile_mode=update` run, which isn't available
while authoring these files outside CI — tracked as a follow-up rather than
hand-authored here.

## Known limitations / follow-ups

This lab currently validates one `cc_library` + `cc_binary(linkshared =
True)` target with a single header-only public surface. It does not yet
cover: `cc_shared_library`, generated headers, multiple libraries, a
consumer binary, a `MODULE.bazel.lock`, or a machine-readable scenario
matrix with an expected/actual oracle per patch (fixtures/patches/scripts
that apply a change to a clean fixture, run a scan, and assert on the JSON
— rather than relying on long-lived PRs and human eyeballing of comments).
A security scenario specifically demonstrating "breaking code change +
modified committed baseline in the same PR still gates red" also hasn't
been exercised as an actual test PR yet — the architecture above (baseline
read from base SHA, `abi/**` in `CODEOWNERS`) should already guarantee it
structurally, but that's a claim, not yet a verified test. These are
tracked as follow-up work rather than folded into this change.
