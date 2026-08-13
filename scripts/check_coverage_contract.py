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
0/0 TUs parsed, 0/6 symbols matched, ..."`. This script regex-extracts
those counts. That's inherently fragile against a detail-string format
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


def _is_build_affecting(changed_files):
    import fnmatch
    return any(
        fnmatch.fnmatch(path, pattern)
        for path in changed_files
        for pattern in _BUILD_AFFECTING_PATTERNS
    )

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


def _extract_l4_selected_tus(coverage):
    """Return the "selected" count from L4_source_abi's "P/S TUs parsed", or None.

    `depth: source` defaults to `scope=changed` replay: only compile units
    whose source actually changed vs. the base are in scope at all. A PR
    that touches only the gate's own YAML/scripts (still correctly judged
    "relevant" by scripts/paths_changed.py -- it needs review, even though
    it isn't C++) has *zero* compile units in scope, so `symbols_matched`
    is inescapably 0/N -- not because linking failed, but because there was
    nothing to link (verified against a real report from this exact
    scenario: "scope=changed, 0/0 TUs parsed, 0/6 symbols matched"). None
    on any parse failure -- the caller must not treat "unknown" the same as
    "confirmed zero": exempting the ratio check needs positive evidence
    that the scope was genuinely empty, not just an absent/reformatted
    detail string (Codex review).
    """
    entry = coverage.get("L4_source_abi")
    if entry is None:
        return None
    m = L4_TUS_RE.search(entry.get("detail", ""))
    if not m:
        return None
    return int(m.group(2))


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
             changed_files=None):
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
        selected_tus = _extract_l4_selected_tus(coverage)
        facts["selected_tus"] = selected_tus
        # Positive evidence required on *both* axes before exempting:
        # a confirmed-empty scope (see _extract_l4_selected_tus) AND a
        # known, exhaustive changed-file list that contains nothing able to
        # reach the compiler. Either one missing/unknown falls through to
        # the ordinary ratio check below -- exemption is an allowlist, not
        # a default (Codex review: an earlier revision exempted on
        # selected_tus==0 alone, which a BUILD.bazel/.bzl/.bazelrc-only PR
        # could also trigger while genuinely changing compiled semantics).
        exemptible = (
            selected_tus == 0
            and changed_files is not None
            and not _is_build_affecting(changed_files)
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
            ratio = (matched / total) if total else 0.0
            facts["export_match_ratio"] = round(ratio, 4)
            if ratio < min_export_match_ratio:
                failures.append(
                    f"export-to-source link ratio {matched}/{total} ({ratio:.0%}) "
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
