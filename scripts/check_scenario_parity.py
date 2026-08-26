#!/usr/bin/env python3
"""Assert one semantic mutation yields one answer across build systems.

`scripts/run_scenario.py --build-system {bazel,cmake,make}` already checks
each run against that scenario's own oracle. Running all three and checking
each separately is NOT parity: all three could pass their own oracle while
disagreeing with each other about which symbols moved, and nothing would
notice. Bazel might report a finding CMake missed, so long as both landed
on the declared verdict.

This compares the reports across build systems instead. For every scenario
that ran under more than one build system, the normalized finding set and
the verdict must be identical. What is allowed to differ is provenance --
paths, build ids, timings, the producing build system's own bookkeeping --
so the comparison is taken over (kind, symbol, severity) triples and the
verdict, never over whole reports.

Usage:
    check_scenario_parity.py --results bazel=out/bazel cmake=out/cmake ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

Finding = Tuple[str, str, str]


class ParityError(RuntimeError):
    """The parity inputs themselves were unusable."""


def normalized_findings(report: Dict[str, Any]) -> Set[Finding]:
    """(kind, symbol, severity) triples -- the semantic content of a report.

    Deliberately excludes everything that legitimately differs by build
    system: file paths, build directories, timings, and any provenance the
    producing toolchain stamped in. A parity check over whole reports would
    fail on those and prove nothing about semantics.
    """
    findings: Set[Finding] = set()
    for change in report.get("changes") or []:
        findings.add(
            (
                str(change.get("kind")),
                str(change.get("symbol")),
                str(change.get("severity")),
            )
        )
    return findings


def normalized_suppressed(report: Dict[str, Any]) -> Set[str]:
    """Symbols a suppression rule accepted -- also semantic, not provenance."""
    return {
        str(change.get("symbol"))
        for change in report.get("suppressed_changes") or []
    }


def load_results(directory: Path) -> Dict[str, Dict[str, Any]]:
    """Every per-scenario report a run_scenario.py results dir contains."""
    if not directory.is_dir():
        raise ParityError(f"{directory}: not a results directory")
    reports = {}
    for path in sorted(directory.glob("*.json")):
        if path.name in {"summary.json", "skipped.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParityError(f"{path}: unreadable report: {exc}") from exc
        if isinstance(payload, dict):
            reports[path.stem] = payload
    return reports


def compare(results: Dict[str, Dict[str, Dict[str, Any]]]) -> List[str]:
    """Compare every scenario across the build systems that produced it."""
    errors: List[str] = []
    if len(results) < 2:
        return [
            "parity needs at least two build systems' results; got "
            f"{sorted(results)} -- a single run cannot disagree with anything"
        ]

    every_scenario = sorted({name for reports in results.values() for name in reports})
    if not every_scenario:
        return ["no scenario reports found in any results directory"]

    compared = 0
    for scenario in every_scenario:
        present = {bs: reports[scenario] for bs, reports in results.items() if scenario in reports}
        if len(present) < 2:
            # Declared for one build system only (see build-matrix.yaml).
            # Not an error; counted so a suite where NOTHING overlaps fails.
            continue
        compared += 1
        reference_bs = sorted(present)[0]
        reference = present[reference_bs]
        ref_findings = normalized_findings(reference)
        ref_suppressed = normalized_suppressed(reference)
        ref_verdict = reference.get("verdict")
        for build_system in sorted(present)[1:]:
            report = present[build_system]
            if report.get("verdict") != ref_verdict:
                errors.append(
                    f"{scenario}: verdict differs -- {reference_bs}="
                    f"{ref_verdict!r} vs {build_system}={report.get('verdict')!r}"
                )
            findings = normalized_findings(report)
            if findings != ref_findings:
                only_ref = sorted(ref_findings - findings)
                only_other = sorted(findings - ref_findings)
                errors.append(
                    f"{scenario}: findings differ -- only in {reference_bs}: "
                    f"{only_ref!r}; only in {build_system}: {only_other!r}"
                )
            suppressed = normalized_suppressed(report)
            if suppressed != ref_suppressed:
                errors.append(
                    f"{scenario}: suppressed symbols differ -- {reference_bs}="
                    f"{sorted(ref_suppressed)!r} vs {build_system}={sorted(suppressed)!r}"
                )
    if compared == 0:
        errors.append(
            "no scenario ran under more than one build system, so nothing was "
            "compared -- this would report parity without checking any"
        )
    return errors


def _parse_results(pairs: List[str]) -> Dict[str, Path]:
    parsed = {}
    for pair in pairs:
        name, sep, path = pair.partition("=")
        if not sep or not name or not path:
            raise ParityError(f"--results expects build-system=path, got {pair!r}")
        parsed[name] = Path(path)
    return parsed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results", nargs="+", required=True,
        help="build-system=results-dir pairs, e.g. bazel=out/bazel cmake=out/cmake",
    )
    parser.add_argument("--out", type=Path, help="write a JSON parity receipt here")
    args = parser.parse_args(argv)
    try:
        directories = _parse_results(args.results)
        results = {name: load_results(path) for name, path in directories.items()}
    except ParityError as exc:
        print(f"ERROR: {exc}")
        return 1

    errors = compare(results)
    receipt = {
        "build_systems": sorted(results),
        "scenarios_per_build_system": {bs: sorted(r) for bs, r in sorted(results.items())},
        "errors": errors,
        "ok": not errors,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        shared = sorted(
            set.intersection(*(set(r) for r in results.values())) if results else set()
        )
        print(
            f"check_scenario_parity: OK -- {len(shared)} scenario(s) agree across "
            f"{', '.join(sorted(results))}"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
