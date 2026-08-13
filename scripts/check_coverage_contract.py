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

## Evidence source

Every field this script reads was verified against a real scan report
downloaded from a completed CI run of this repo's own `scan` job (not
guessed): `coverage` is a list of `{layer, status, confidence?, detail,
elapsed_s?}` objects. `L3_build`/`L4_source_abi` don't carry structured
counts for target/symbol matching -- only free-text `detail` strings, e.g.
`"bazel, 1 compile units, 0 targets"` and `"scope=changed, 0/0 TUs parsed,
0/6 symbols matched, ..."`. This script regex-extracts those counts. That's
inherently fragile against a detail-string format change upstream --
documented here, not hidden: if the pattern stops matching, this script
fails closed (treats the requirement as unmet) rather than silently
passing, since a coverage gate that can't read its own evidence must not
default to trusting it.
"""
import argparse
import json
import re
import sys

L3_BUILD_RE = re.compile(r"(\d+)\s+compile\s+units?,\s+(\d+)\s+targets?")
L4_SYMBOLS_RE = re.compile(r"(\d+)/(\d+)\s+symbols\s+matched")

# crosscheck layers that report a fixed reason when public-header
# provenance wasn't supplied -- verified against the real report.
_NO_PROVENANCE_REASON = "no public-header provenance"


def _coverage_by_layer(report):
    return {c.get("layer"): c for c in report.get("coverage", []) if isinstance(c, dict)}


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


def _has_public_header_provenance(coverage):
    # Fail closed, not open: require *positive* evidence of provenance,
    # not just the absence of a specific skip reason. If a degraded scan
    # emits no crosscheck: layers at all (schema change, renamed layers,
    # a crosscheck stage that didn't run), there is no evidence either
    # way -- treating that as "provenance present" would be exactly the
    # silent-pass-on-missing-evidence bug this whole script exists to
    # close (CodeRabbit + Codex review).
    crosschecks = [layer for layer in coverage if layer and layer.startswith("crosscheck:")]
    if not crosschecks:
        return False
    skipped_for_provenance = [
        layer for layer in crosschecks
        if coverage[layer].get("status") == "skipped"
        and _NO_PROVENANCE_REASON in coverage[layer].get("detail", "")
    ]
    return not skipped_for_provenance


def evaluate(report, *, requested_depth, min_compile_units, require_bazel_target,
             require_public_header_provenance, min_export_match_ratio):
    coverage = _coverage_by_layer(report)
    level = report.get("level", {}) or {}
    effective_depth = level.get("depth")

    failures = []
    facts = {"requested_depth": requested_depth, "effective_depth": effective_depth}

    if effective_depth != requested_depth:
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
    args = parser.parse_args()

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
