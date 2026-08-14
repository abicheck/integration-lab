#!/usr/bin/env python3
"""Enforce a coverage contract for `depth: source` ABICheck scans.

The problem this closes (P0 review item #2): asking abicheck for
`depth: source` only asks it to *attempt* source-level evidence. When that
attempt degrades -- 0 Bazel targets resolved, 0/6 exported symbols linked
back to source, no public-header provenance -- abicheck v0.5.0 still
reports a plain `COMPATIBLE` verdict at exit code 0. The tool's own report
says the requested depth wasn't actually achieved (in `advisories` and in
the `coverage` layer entries), but nothing in the pipeline acts on that.

abicheck v0.5.0 has no first-class way to gate on evidence completeness --
that's `--contract-evaluation` (ADR-049), which doesn't exist until
`abicheck/main` (see the `canary` job in abi-scan.yml, which previews it,
non-blocking). Until the stable release has it, this script is the
lab-side stand-in the review explicitly calls for: read the same
`abicheck-report.json` the gate already produced, and independently assert
on its coverage evidence.

Also used against `baseline.yml`'s raw `dump` output before it's committed
(with `--no-require-public-header-provenance`, since `dump` mode never
runs crosschecks at all) -- a good Bazel evidence *input* doesn't
guarantee the dump itself achieved good coverage, so the same validation
applies on both sides of every comparison, not just the PR side.

## Evidence source

Every field this script reads was verified against real reports downloaded
from completed CI runs of this repo's own `scan` job and against the real
committed baseline (not guessed). Two shapes, both handled (see
`_coverage_by_layer`): a `scan`-mode report has `coverage` as a top-level
list; a `dump`-mode snapshot has the same shape nested at
`build_source.manifest.coverage`, and no top-level `coverage` or `level`
key at all. Either way, each entry is a `{layer, status, confidence?,
detail, elapsed_s?}` object. `L3_build`/`L4_source_abi` don't carry
structured counts for target/symbol matching -- only free-text `detail`
strings, e.g. `"bazel, 1 compile units, 0 targets"` and `"scope=changed,
0/0 TUs parsed, 0/6 symbols matched, 3/6 accounted, 6 unmatched, ..."`.
This script regex-extracts those counts. That's inherently fragile against
a detail-string format
change upstream -- documented here, not hidden: if the pattern stops
matching, this script fails closed (treats the requirement as unmet)
rather than silently passing, since a coverage gate that can't read its
own evidence must not default to trusting it.

## The empty-changed-scope exemption

`depth: source` on the `scan` side defaults to `scope=changed` replay: a
PR touching only this gate's own workflow/scripts has zero compile units
in scope, so `export_match_ratio` is inescapably 0/N with nothing actually
broken. That's exempted -- but only when `--changed-files` is supplied
*and* none of those paths can reach the compiler (`_is_build_affecting`);
a BUILD.bazel/`.bzl`/`.bazelrc`-only PR could change compiled semantics
(a new `-D` define flipping `#ifdef`-guarded declarations) without
touching a single `.cc`/`.h` file, so an empty changed-scope there does
NOT mean "nothing to check". Omitting `--changed-files` entirely (as the
baseline validation above does) always enforces the ratio check normally.
"""
import argparse
import json
import re
import sys

L3_BUILD_RE = re.compile(r"(\d+)\s+compile\s+units?,\s+(\d+)\s+targets?")
L4_SYMBOLS_RE = re.compile(r"(\d+)/(\d+)\s+symbols\s+matched")
L4_ACCOUNTED_RE = re.compile(r"(\d+)/(\d+)\s+accounted")
L4_TUS_RE = re.compile(r"(\d+)/(\d+)\s+TUs\s+parsed")

# Deliberately duplicated from scripts/paths_changed.py's own pattern list
# (kept as a small, independently-readable constant here rather than an
# import-path hack across two standalone, individually-invokable scripts):
# these are the paths that can actually reach the compiler -- a Bazel rule,
# a compile flag, an include root, the source itself. Everything else
# scripts/paths_changed.py calls "relevant" (workflows, this repo's own
# gate scripts, CODEOWNERS, docs) participates in "does the gate need to
# run" but can never change what gets compiled. The changed-source-scope
# exemption below must NEVER apply when a file in this set changed: e.g. a
# new `-D` define added to BUILD.bazel could flip `#ifdef`-guarded
# declarations without touching a single .cc/.h file, so `scope=changed`
# selecting 0 TUs there does not mean "nothing to check" the way it does
# for a workflow-only diff (Codex review).
#
# Deliberately NOT the same set as paths_changed.py's relevance list:
# abi/** is relevant there (a baseline edit needs the gate to run, and
# CODEOWNERS review) but isn't compiler-affecting -- it's the trusted JSON
# snapshot the *comparison* reads, never an input to the Bazel build graph.
# Including it here would deny the exemption to a PR that only corrects
# abi/math.abicheck.json, forcing a reviewed baseline-only fix to fail its
# own export-match-ratio check for no reason connected to compilation
# (Codex review, fresh evidence).
_BUILD_AFFECTING_PATTERNS = (
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
    ".abicheck.yml",
    # Repo-wide backstop, same reasoning and same set as
    # scripts/paths_changed.py's PATTERNS: Bazel doesn't enforce any
    # particular directory layout, so a source/header referenced from
    # outside include/*/src/* (e.g. a future config/ subpackage) must
    # still disqualify the exemption -- matching by extension repo-wide
    # closes that regardless of where the file lives (Codex review, fresh
    # evidence after the abi/* fix above).
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
)


# Codex review (P1), fresh evidence: without this, a PR mixing a fixture
# change with a genuinely relevant path (e.g. this file, or
# .github/workflows/abi-scan.yml itself -- exactly the shape of commits
# this repo's own architecture-review PR made throughout) would still see
# its fixtures/*.cc paths classified as build-affecting here, denying the
# empty-scope exemption on any future diff where scope=changed's replay
# selection legitimately resolves to 0 TUs (nothing in //:math's own
# source changed). Same exclusion, same reasoning, as
# scripts/paths_changed.py's EXCLUDED_PREFIXES -- duplicated rather than
# imported, matching this pair of scripts' existing convention of keeping
# their pattern lists as independent, doc-linked supersets rather than a
# shared module (see this file's own docstring on that invariant).
_EXCLUDED_PREFIXES = (
    "fixtures/",
    "scenarios/",
    "suppressions/",
)


# Repo-wide: these affect EVERY target's toolchain/build configuration
# regardless of which Bazel package they live in (a bzlmod dependency
# bump, a global copt in .bazelrc, a Bazel version pin), so package
# awareness below never applies to them -- always disqualifying.
_REPO_WIDE_BUILD_AFFECTING_PATTERNS = (
    "MODULE.bazel",
    "MODULE.bazel.lock",
    ".bazelrc",
    ".bazelversion",
    ".abicheck.yml",
)

# Package-scoped: a BUILD.bazel/BUILD/.bzl file only disqualifies the
# exemption when its OWN package is one `deps(//:math)` actually resolves
# through -- see _is_build_affecting's own docstring for why (P1-1,
# architecture review: this repo's own facts.bzl/BUILD.bazel additions
# under tools/abicheck/ are a real, first-hand example of a Bazel-file
# change that structurally cannot affect `:math`'s own compilation, since
# nothing in `:math`'s dependency closure references that package at all).
_PACKAGE_SCOPED_BUILD_AFFECTING_PATTERNS = (
    "BUILD.bazel",
    "*/BUILD",
    "*/BUILD.bazel",
    "*.bzl",
)

# Real C/C++ source patterns stay unconditionally disqualifying, same as
# before -- unlike a BUILD.bazel/.bzl file, a source/header's mere
# existence outside deps(//:math)'s resolved *target* set says nothing
# about whether it's reachable from //:math's own compile (Bazel doesn't
# require every included header to be its own declared target), so
# there's no package-membership signal here that's safe to trust the way
# there is for a BUILD-graph file.
_SOURCE_BUILD_AFFECTING_PATTERNS = (
    "include/*",
    "src/*",
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
)

assert set(_BUILD_AFFECTING_PATTERNS) == (
    set(_REPO_WIDE_BUILD_AFFECTING_PATTERNS)
    | set(_PACKAGE_SCOPED_BUILD_AFFECTING_PATTERNS)
    | set(_SOURCE_BUILD_AFFECTING_PATTERNS)
), "the three-way split above must stay a partition of _BUILD_AFFECTING_PATTERNS"


def _label_package(label):
    """The Bazel package portion of a resolved target label, or None for
    an external-repo label (`@repo//pkg:name`) -- a changed file path from
    a PR diff is always workspace-relative, so it can never equal an
    external repo's own package and there's nothing useful to compare
    against.

    `//pkg:name` -> `"pkg"`; `//:name` (root package) -> `""`.
    """
    if not label.startswith("//"):
        return None
    pkg = label[2:].split(":", 1)[0]
    return pkg


def _cquery_packages(cquery_path):
    """The set of Bazel packages `deps(//:math)`'s own cquery JSON output
    resolved through, or None if unreadable/malformed -- None (not an
    empty set) so the caller can tell "no package data available, fall
    back to the fully conservative check" apart from "resolved packages,
    and they're genuinely all outside this diff", matching this file's
    fail-closed convention everywhere else (Codex review reasoning,
    applied here too: an empty set would make EVERY BUILD.bazel/.bzl file
    look safe to exempt, which is exactly backwards for missing data).
    """
    try:
        with open(cquery_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    packages = set()
    for result in data.get("results", []):
        rule = result.get("target", {}).get("rule")
        if not rule or "name" not in rule:
            continue
        pkg = _label_package(rule["name"])
        if pkg is not None:
            packages.add(pkg)
    return packages


def _file_package(path):
    """The Bazel package a file belongs to, by simple containing-directory
    convention (matching how BUILD.bazel/.bzl package membership actually
    works) -- a root-level file has package `""`.
    """
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _is_build_affecting(changed_files, target_packages=None):
    """True if any path in `changed_files` could plausibly change what
    `//:math` compiles to.

    `target_packages`, when given (from `_cquery_packages`), narrows the
    package-scoped patterns (`BUILD.bazel`/`*.bzl`/...): a matching file
    only disqualifies the exemption when its OWN package is one
    `deps(//:math)` actually resolves through -- Bazel's own dependency
    graph is strong, structural evidence that a package OUTSIDE that set
    cannot affect `:math`'s compilation, regardless of what changed inside
    it. `target_packages is None` (cquery data unavailable, or the caller
    didn't supply it) falls back to the original, fully conservative
    behavior: every package-scoped pattern match disqualifies, same as
    before this refinement (fail closed on missing evidence, matching
    every other extractor in this file). Repo-wide and source patterns are
    never narrowed by this -- see their own pattern-list comments for why.
    """
    import fnmatch

    def _matches(path, patterns):
        return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)

    for path in changed_files:
        if any(path.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
            continue
        if _matches(path, _REPO_WIDE_BUILD_AFFECTING_PATTERNS):
            return True
        if _matches(path, _SOURCE_BUILD_AFFECTING_PATTERNS):
            return True
        if _matches(path, _PACKAGE_SCOPED_BUILD_AFFECTING_PATTERNS):
            if target_packages is None or _file_package(path) in target_packages:
                return True
    return False

# crosscheck layers that require public-header provenance to run at all --
# verified against the real report: with no --public-header/--public-header-dir
# supplied, all four report status "skipped", detail "no public-header
# provenance (supply --public-header/--public-header-dir ...)". Other
# crosscheck: layers (header_build_context_mismatch, unversioned_exported_symbol,
# odr_type_variant, ...) run independently of header provenance and don't
# belong in this set -- their status says nothing about provenance.
_PROVENANCE_GATED_LAYERS = frozenset({
    "crosscheck:exported_not_public",
    "crosscheck:public_not_exported",
    "crosscheck:private_header_leak",
    "crosscheck:rtti_for_internal_type",
})


def _coverage_by_layer(report):
    # Two real, verified shapes: a `scan`-mode report has `coverage` as a
    # top-level list (`ScanOutcome.to_dict()`); a `dump`-mode snapshot (the
    # format baseline.yml commits) has no top-level `coverage` at all --
    # the same layer/status/detail-shaped list lives at
    # `build_source.manifest.coverage` instead (verified against the real
    # committed abi/math.abicheck.json: identical `{layer, status,
    # confidence, detail, elapsed_s}` entries for L3_build/L4_source_abi/
    # L5_source_graph). Falls back to the nested path only when the
    # top-level key is absent/empty, so a scan report's own (possibly
    # legitimately empty) top-level list is never masked by a coincidental
    # nested one.
    coverage = report.get("coverage")
    if not coverage:
        coverage = (
            report.get("build_source", {})
            .get("manifest", {})
            .get("coverage", [])
        )
    return {c.get("layer"): c for c in coverage if isinstance(c, dict)}


def _extract_l3(coverage):
    entry = coverage.get("L3_build")
    if entry is None:
        return None, None, "L3_build coverage entry missing"
    m = L3_BUILD_RE.search(entry.get("detail", ""))
    if not m:
        return None, None, f"could not parse L3_build detail: {entry.get('detail')!r}"
    return int(m.group(1)), int(m.group(2)), None


def _extract_l4(coverage):
    entry = coverage.get("L4_source_abi")
    if entry is None:
        return None, None, "L4_source_abi coverage entry missing"
    m = L4_SYMBOLS_RE.search(entry.get("detail", ""))
    if not m:
        return None, None, f"could not parse L4_source_abi detail: {entry.get('detail')!r}"
    return int(m.group(1)), int(m.group(2)), None


def _extract_l4_accounted(coverage):
    """Return (accounted, total) from L4_source_abi's "A/Y accounted", or (None, None).

    `matched`/`total` (see `_extract_l4`) counts every exported symbol in
    the denominator, including ones abicheck itself classifies as
    non-public (ELF-linker artifacts like `_end`/`_edata`/`__bss_start`,
    and hidden-visibility compiler-generated special members) -- those can
    never be linked back to source, by construction, no matter how
    complete the real public-API evidence is. Verified against the real
    committed baseline (`abi/math.abicheck.json`): of 6 "exported"
    symbols, 3 are classified non-public, capping `matched/total` at 50%
    forever regardless of how good the public-function linkage actually
    is (Codex review, fresh evidence -- "Exclude classified internal
    exports from the match ratio").

    `accounted` is a *third* count abicheck reports alongside `matched`
    and `total`: symbols that are either matched to source OR explicitly
    classified as non-public, i.e. symbols whose coverage status is fully
    explained one way or the other. `total - accounted` is the number of
    exported symbols with no explanation at all -- neither linked to
    source nor classified as internal -- which is the real evidence gap
    this contract should be gating on. (None, None) on any parse failure,
    same fail-closed contract as every other extractor here: the caller
    must fall back to the stricter `matched/total` basis, not silently
    treat missing evidence as 100% accounted.
    """
    entry = coverage.get("L4_source_abi")
    if entry is None:
        return None, None
    m = L4_ACCOUNTED_RE.search(entry.get("detail", ""))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _extract_l4_tus(coverage):
    """Return (parsed, selected) from L4_source_abi's "P/S TUs parsed", or (None, None).

    `depth: source` defaults to `scope=changed` replay: only compile units
    whose source actually changed vs. the base are in scope (selected) at
    all. A PR that touches only the gate's own YAML/scripts (still
    correctly judged "relevant" by scripts/paths_changed.py -- it needs
    review, even though it isn't C++) has *zero* compile units selected,
    so `symbols_matched` is inescapably 0/N -- not because linking failed,
    but because there was nothing to link (verified against a real report
    from this exact scenario: "scope=changed, 0/0 TUs parsed, 0/6 symbols
    matched"). (None, None) on any parse failure -- the caller must not
    treat "unknown" the same as "confirmed zero": exempting the ratio
    check needs positive evidence that the scope was genuinely empty, not
    just an absent/reformatted detail string (Codex review).

    `parsed` can be *less than* `selected` -- a TU was selected (its
    source changed) but the replay failed to actually parse it (e.g. a
    clang crash, an unsupported construct). That's a real coverage gap
    distinct from the exemption above: any source-only API change inside
    that specific unparsed TU is invisible to this scan, even if every
    *other* selected TU parsed cleanly and the overall symbol-match ratio
    looks fine (Codex review, fresh evidence -- a prior revision only
    ever read `selected`, never checking `parsed` against it).
    """
    entry = coverage.get("L4_source_abi")
    if entry is None:
        return None, None
    m = L4_TUS_RE.search(entry.get("detail", ""))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _has_public_header_provenance(coverage):
    # Fail closed, not open: require *positive* evidence of provenance --
    # at least one provenance-gated crosscheck actually completed -- not
    # just the absence of one specific skip phrase, and not just "anything
    # other than skipped". An earlier revision checked `status != "skipped"`,
    # which is a denylist: it would also accept a hypothetical "failed"/
    # "error"/"partial" status (or simply a missing status key) as positive
    # evidence, since none of those equal "skipped" either (Codex review).
    # Verified against abicheck's own schema (buildsource/crosscheck_base.py,
    # `_CheckOutput.status: str # "present" | "skipped"` -- every call site
    # in crosscheck.py/crosscheck_coherence.py only ever constructs one of
    # those two) that "present" is the complete, exact success value, so
    # this now allowlists it explicitly instead of denylisting "skipped".
    # And if none of the provenance-gated layers appear at all (schema
    # change, renamed layers, a crosscheck stage that didn't run), there is
    # no evidence either way -- treating that as "provenance present" would
    # be the same silent-pass-on-missing-evidence bug (CodeRabbit + Codex
    # review, first round).
    crosschecks = [layer for layer in coverage if layer in _PROVENANCE_GATED_LAYERS]
    if not crosschecks:
        return False
    return any(coverage[layer].get("status") == "present" for layer in crosschecks)


def evaluate(report, *, requested_depth, min_compile_units, require_bazel_target,
             require_public_header_provenance, min_export_match_ratio,
             changed_files=None, target_packages=None):
    coverage = _coverage_by_layer(report)
    # `level.depth` only exists on a `scan`-mode report. `dump`-mode
    # snapshots (baseline.yml) have no `level` key at all -- absent, not
    # "different depth achieved" -- so this check only fires when the key
    # is actually present, avoiding a spurious depth-mismatch failure on
    # every baseline validation (Codex review: this script needs to also
    # validate the baseline's own dump output, which has no such field).
    level = report.get("level")
    effective_depth = (level or {}).get("depth")

    failures = []
    facts = {"requested_depth": requested_depth, "effective_depth": effective_depth}

    if level is not None and effective_depth != requested_depth:
        failures.append(
            f"requested depth '{requested_depth}' but effective depth was '{effective_depth}'"
        )

    compile_units, bazel_targets, l3_err = _extract_l3(coverage)
    facts["compile_units"] = compile_units
    facts["bazel_targets"] = bazel_targets
    if l3_err:
        failures.append(l3_err)
    else:
        if compile_units < min_compile_units:
            failures.append(f"compile_units {compile_units} < required minimum {min_compile_units}")
        if require_bazel_target and bazel_targets < 1:
            failures.append("no Bazel target resolved (0 targets) -- source evidence isn't linked to a build target")

    matched, total, l4_err = _extract_l4(coverage)
    facts["symbols_matched"] = matched
    facts["symbols_total"] = total
    if l4_err:
        failures.append(l4_err)
    else:
        parsed_tus, selected_tus = _extract_l4_tus(coverage)
        facts["parsed_tus"] = parsed_tus
        facts["selected_tus"] = selected_tus
        # A selected TU that never actually parsed is a real coverage gap
        # regardless of the exemption below or the ratio check that
        # follows: any source-only API change inside that specific TU is
        # invisible to this scan, even when the overall symbol-match ratio
        # happens to look fine because the *other* selected TUs parsed
        # cleanly (Codex review -- an earlier revision only ever read
        # `selected`, never checking `parsed` against it).
        if (
            parsed_tus is not None
            and selected_tus is not None
            and parsed_tus < selected_tus
        ):
            failures.append(
                f"only {parsed_tus}/{selected_tus} selected translation unit(s) "
                "actually parsed -- a source-only change in an unparsed TU "
                "would be invisible to this scan"
            )
        # Positive evidence required on *both* axes before exempting:
        # a confirmed-empty scope (see _extract_l4_tus) AND a
        # known, exhaustive changed-file list that contains nothing able to
        # reach the compiler. Either one missing/unknown falls through to
        # the ordinary ratio check below -- exemption is an allowlist, not
        # a default (Codex review: an earlier revision exempted on
        # selected_tus==0 alone, which a BUILD.bazel/.bzl/.bazelrc-only PR
        # could also trigger while genuinely changing compiled semantics).
        exemptible = (
            selected_tus == 0
            and changed_files is not None
            and not _is_build_affecting(changed_files, target_packages)
        )
        if exemptible:
            # Confirmed empty scope=changed replay over a confirmed
            # non-build-affecting diff -- 0/N isn't a failed link, there
            # was nothing in scope to link. Not a gate failure; recorded as
            # a fact, not folded into export_match_ratio, so a reader can't
            # mistake "exempt" for "100% matched".
            facts["export_match_ratio"] = None
            facts["export_match_ratio_exempt_reason"] = (
                "0 compile units in scope=changed replay, and no changed "
                "file can reach the compiler -- this diff doesn't touch "
                "any C++ source or build configuration, so there's "
                "nothing to link"
            )
        else:
            # Prefer `accounted` (matched + classified-non-public) over raw
            # `matched` as the ratio's numerator -- see _extract_l4_accounted
            # for why: a symbol abicheck itself classifies as non-public
            # (ELF-linker artifacts, hidden-visibility compiler-generated
            # special members) can never be linked to source, so counting
            # it as an unmatched failure caps the ratio below 100% forever,
            # independent of real public-API linkage quality (Codex review,
            # fresh evidence). Only trusted when `accounted`'s own total
            # agrees with `matched`'s total -- a mismatch means the two
            # counts came from different underlying symbol sets (schema
            # drift) and accounted can't be safely substituted; falls back
            # to the stricter raw `matched/total` basis instead, same
            # fail-closed contract as everywhere else in this script.
            accounted, accounted_total = _extract_l4_accounted(coverage)
            facts["symbols_accounted"] = accounted
            if accounted is not None and accounted_total == total:
                ratio_numerator = accounted
                ratio_basis = "accounted"
            else:
                ratio_numerator = matched
                ratio_basis = "matched"
            facts["export_match_ratio_basis"] = ratio_basis
            ratio = (ratio_numerator / total) if total else 0.0
            facts["export_match_ratio"] = round(ratio, 4)
            if ratio < min_export_match_ratio:
                failures.append(
                    f"export-to-source link ratio ({ratio_basis}) "
                    f"{ratio_numerator}/{total} ({ratio:.0%}) "
                    f"< required minimum {min_export_match_ratio:.0%}"
                )

    has_provenance = _has_public_header_provenance(coverage)
    facts["public_header_provenance"] = has_provenance
    if require_public_header_provenance and not has_provenance:
        failures.append("no public-header provenance (crosscheck layers skipped: supply --public-header/--public-header-dir)")

    contract_met = not failures
    result = {
        "requested_depth": requested_depth,
        "facts": facts,
        "failures": failures,
        "analysis_status": "COMPLETE" if contract_met else "INCOMPLETE",
        "gate_status": "PASS" if contract_met else "FAIL",
        "compatibility_verdict": report.get("verdict") if contract_met else "NOT_FULLY_EVALUATED",
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="Path to the scan JSON report to evaluate")
    parser.add_argument("-o", "--output", required=True, help="Path to write the contract result JSON")
    parser.add_argument("--requested-depth", default="source")
    parser.add_argument("--min-compile-units", type=int, default=1)
    parser.add_argument(
        "--require-bazel-target", action=argparse.BooleanOptionalAction, default=True,
    )
    parser.add_argument(
        "--require-public-header-provenance", action=argparse.BooleanOptionalAction, default=True,
    )
    parser.add_argument("--min-export-match-ratio", type=float, default=0.95)
    parser.add_argument(
        "--changed-files", default="",
        help="Path to a newline-separated list of changed files (e.g. from "
        "`git diff --name-only`), required for the empty-changed-scope "
        "export-match-ratio exemption to ever apply. Omit to always "
        "enforce the ratio check (the pre-exemption behavior).",
    )
    parser.add_argument(
        "--cquery", default="",
        help="Path to `bazel cquery --output=jsonproto 'deps(//:math)'` "
        "JSON output. Narrows the exemption's BUILD.bazel/.bzl "
        "disqualification to only the packages //:math's own dependency "
        "closure actually resolves through (see _is_build_affecting's own "
        "docstring). Omit to fall back to the original, fully conservative "
        "behavior -- any BUILD.bazel/.bzl change anywhere disqualifies.",
    )
    args = parser.parse_args()

    changed_files = None
    if args.changed_files:
        try:
            with open(args.changed_files, "r", encoding="utf-8") as fh:
                changed_files = [line.strip() for line in fh if line.strip()]
        except OSError:
            # Unreadable list is "unknown", not "empty" -- an empty list
            # would incorrectly satisfy _is_build_affecting's "none of
            # these match" check and wrongly permit the exemption. None
            # correctly falls through to the ordinary ratio check instead.
            changed_files = None

    target_packages = _cquery_packages(args.cquery) if args.cquery else None

    try:
        with open(args.report, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        result = {
            "requested_depth": args.requested_depth,
            "facts": {},
            "failures": [f"could not read report: {exc}"],
            "analysis_status": "INCOMPLETE",
            "gate_status": "FAIL",
            "compatibility_verdict": "NOT_FULLY_EVALUATED",
        }
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        return 1

    result = evaluate(
        report,
        requested_depth=args.requested_depth,
        min_compile_units=args.min_compile_units,
        require_bazel_target=args.require_bazel_target,
        require_public_header_provenance=args.require_public_header_provenance,
        min_export_match_ratio=args.min_export_match_ratio,
        changed_files=changed_files,
        target_packages=target_packages,
    )

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    if result["gate_status"] != "PASS":
        for failure in result["failures"]:
            print(f"::error::coverage contract: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
