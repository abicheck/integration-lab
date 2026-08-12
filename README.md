# ABICheck Bazel Lab

A small public C++ shared-library project used to validate ABICheck GitHub Actions.

- `baseline.yml` builds `//:math` on `main`, collects a source-aware ABI
  snapshot, normalizes it (see below), and commits it to
  `abi/math.abicheck.json` if the normalized content changed.
- `abi-scan.yml` runs on pull requests, builds the candidate with Bazel,
  resolves the trusted baseline from the PR's exact base commit, and runs a
  **single** ABICheck source scan that is simultaneously the merge gate,
  the sticky PR comment, and the job summary.

Test PRs intentionally exercise compatible additions, binary ABI breaks,
source/API breaks, and implementation-only changes.

## Gate / PR-comment architecture

The PR workflow runs exactly one `abicheck` invocation (`mode: scan`,
`depth: source`, `format: json`). Its outcome drives all of the following,
so there is one verdict and one report, not two analyses that can disagree:

- pass/fail gate (`fail-on-breaking`, `fail-on-api-break`)
- the sticky PR comment (`pr-comment: true`)
- the job summary
- the `abicheck-report` artifact, uploaded with `if: always()` so it is
  available even when the gate fails

The baseline it compares against is read directly out of git history at the
PR's exact base SHA (`git show <base-sha>:abi/math.abicheck.json`), not from
the working tree. A PR that edits `abi/math.abicheck.json` in the same diff
that breaks the ABI cannot pass by comparing itself against its own edited
baseline — the trusted side of the comparison always comes from `main` as it
stood before the PR branched.

`since` is likewise pinned to `github.event.pull_request.base.sha` rather
than a moving branch ref, so the analysis is reproducible even if `main`
advances while the PR's checks are running.

## Baseline determinism

Raw `abicheck --mode dump` output embeds run-specific noise: wall-clock
timestamps, absolute runner paths (`/home/runner/work/...`,
`/home/runner/.cache/bazel/...`), filesystem mtimes, and per-run extractor
timings. Left in place, every baseline refresh would produce a commit even
when the public ABI didn't change.

`scripts/normalize_baseline.py` runs between `dump` and the commit step and:

- drops volatile keys (`created_at`, `source_mtime*`, `source_size`, `build_id`)
  wherever they occur in the report
- rewrites absolute paths under the repo checkout to repo-relative paths,
  and absolute paths into the Bazel execroot/output tree to their
  `bazel-out/...`-relative fragment
- collapses `<N>s` timing fragments in extractor `detail` prose to a
  constant placeholder

so that two builds of the same commit on different runners/machines
normalize to byte-identical JSON. `git_commit`/`version` are intentionally
left untouched — they're meant to track the `main` SHA the baseline was
collected from, not to be stripped as noise.

## Known limitations / follow-ups

This lab currently validates one `cc_library` + `cc_binary(linkshared =
True)` target with a single header-only public surface. It does not yet
cover: `cc_shared_library`, generated headers, multiple libraries, a
consumer binary, or a machine-readable scenario matrix with an
expected/actual oracle per patch. Those are tracked as follow-up work
rather than folded into this change.
