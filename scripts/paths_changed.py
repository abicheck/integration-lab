#!/usr/bin/env python3
"""Decide whether a PR's changed files are relevant to the ABI scan gate.

The problem this closes: `.github/workflows/abi-scan.yml` used to filter
its trigger with `on.pull_request.paths`. GitHub's own docs are explicit
about the consequence: a workflow skipped by path filtering leaves any
branch-protection-required check it would have produced sitting at
"Pending" forever -- not skipped, not passing, just permanently blocking
merge. Expanding the filter to a superset of `.github/CODEOWNERS`'s paths
(an earlier fix on this branch) only closes that trap for the specific
files this repo's own gate cares about; a PR touching README.md,
.gitignore, a test file, or any other ordinary path never listed here
still triggers nothing at all (Codex review, fresh evidence after that
fix).

The actual fix: the workflow now has no `paths:` filter at all, so it
always triggers and the `scan` job -- and the required check GitHub
attaches to it -- always resolves to some real conclusion. This script
runs *inside* the job instead, deciding whether the PR's changed files
touch anything ABI-relevant; when they don't, the job's expensive
Bazel-build/scan steps are skipped (job-level `if:` conditions keyed off
this script's exit code) and the job succeeds immediately with nothing to
enforce -- never "pending", never a wasted 10-minute scan on a doc-only
PR either.

Patterns are matched with `fnmatch`, which (unlike `glob.glob`) already
treats `*` as matching across `/` -- so `include/*` matches
`include/abicheck_lab/math.h` and `*/BUILD` matches `a/b/BUILD` without
needing a literal `**`. Keep this list a superset of
`.github/CODEOWNERS`'s protected paths, same as the old workflow-level
filter had to be.

Also keep the C/C++-extension patterns (`*.h`/`*.cc`/etc.) a superset of
`check_coverage_contract.py`'s `_BUILD_AFFECTING_PATTERNS` -- both lists
exist to answer "can this file reach the compiler", just for different
purposes (does the gate need to run at all, vs. can the empty-changed-
scope ratio exemption apply), and a file that reaches the compiler must
never be missing from either (Codex review).
"""
import fnmatch
import sys

#: Trees that are ABI-relevant in the generic sense (real C++ source, real
#: BUILD files) but deliberately NOT relevant to *this* gate -- they belong
#: to //:math's own independent validation platform (fixtures/ + scenarios/
#: + suppressions/, exercised by the separate scenarios.yml workflow, never
#: built into //:math's own target graph) rather than to the library this
#: gate actually scans. Without this exclusion, a PR touching ONLY one of
#: these trees (no other ABI-relevant path) would still mark itself
#: relevant via the generic *.h/*.cc/*/BUILD.bazel patterns below, trigger
#: the full required Bazel+depth:source scan, and then fail it: the Bazel
#: evidence pack is scoped to deps(//:math) (abi-scan.yml's own comment on
#: why), so scope=changed's replay selection resolves to 0 TUs for a diff
#: that never touches //:math's own exports -- and check_coverage_contract.py's
#: empty-scope exemption requires the changed-file list to be free of
#: build-affecting paths too, which a fixtures/*.cc change trivially isn't
#: (same *.h/*.cc extension match, no path-prefix awareness there either).
#: Net effect without this exclusion: 0/N export-match ratio, gate fails,
#: for a change this repo's OWN validation platform already independently
#: proved correct (Codex review, fresh evidence -- traced through
#: check_coverage_contract.py's exact exemption logic; not yet observed in
#: this PR's own CI only because every commit here also touched a
#: genuinely //:math-relevant path in the same cumulative diff).
EXCLUDED_PREFIXES = (
    "fixtures/",
    "scenarios/",
    "suppressions/",
)


def _is_excluded(path):
    return any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


PATTERNS = (
    "BUILD.bazel",
    "MODULE.bazel",
    "MODULE.bazel.lock",
    "*/BUILD",
    "*/BUILD.bazel",
    "*.bzl",
    ".bazelrc",
    ".bazelversion",
    "include/*",
    "src/*",
    "abi/*",
    ".abicheck.yml",
    # Repo-wide backstop, not just include/*+src/*: Bazel doesn't enforce
    # any particular directory layout -- a BUILD.bazel target can list a
    # source/header from anywhere in the tree (e.g. a future config/
    # subpackage). A change to such a file would otherwise match no
    # pattern here at all and get silently judged irrelevant -- worse than
    # the "Pending forever" trap this script exists to close, since a
    # relevant change wouldn't even show as pending, it just wouldn't
    # trigger a scan at all (Codex review, fresh evidence). Matching by
    # extension repo-wide is the practical fix short of a live `bazel
    # query` in this step (which would need Bazel set up before the cache
    # step that currently follows it).
    "*.h",
    "*.hh",
    "*.hpp",
    "*.hxx",
    "*.inc",
    "*.c",
    "*.C",
    "*.cc",
    "*.cpp",
    "*.cxx",
    "*.c++",
    ".github/workflows/abi-scan.yml",
    ".github/workflows/baseline.yml",
    # Phase 3: the "Skip if..."/"Resolve trusted baseline" steps this very
    # script's own decision feeds into now live here (extracted from
    # abi-scan.yml itself into two composite actions) -- a PR touching
    # only one of these, on its own, must still be judged relevant so the
    # gate actually exercises the changed code, matching the CODEOWNERS
    # protection just added for the same paths (Codex review, PR #16).
    ".github/actions/*",
    ".github/CODEOWNERS",
    "scripts/normalize_baseline.py",
    "scripts/render_scan_comment.py",
    "scripts/render_conformance_report.py",
    "scripts/render_aggregate_summary.py",
    "scripts/check_coverage_contract.py",
    "scripts/paths_changed.py",
    "scripts/build_bazel_evidence_pack.py",
    "scripts/merge_abicheck_facts.py",
)


def is_relevant(changed_files, patterns=PATTERNS):
    return any(
        fnmatch.fnmatch(path, pattern)
        for path in changed_files
        if not _is_excluded(path)
        for pattern in patterns
    )


def main():
    if len(sys.argv) == 2:
        with open(sys.argv[1], "r", encoding="utf-8") as fh:
            changed_files = [line.strip() for line in fh if line.strip()]
    else:
        changed_files = [line.strip() for line in sys.stdin if line.strip()]

    return 0 if is_relevant(changed_files) else 1


if __name__ == "__main__":
    sys.exit(main())
