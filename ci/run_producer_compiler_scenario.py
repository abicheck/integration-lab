#!/usr/bin/env python3
"""Prove producer-compiler attribution across otherwise identical profiles.

Every build profile in this lab is a GCC 14 profile: they vary by build
system, not by the compiler that actually produces the ABI.  That leaves an
untested claim -- that a finding is attributed to the profiles whose
*producer* it affects, and withheld from the ones it does not.

This scenario isolates that axis.  One fixture, one build system, two
producer compilers, and a change confined to the non-Clang preprocessor
branch:

    #ifdef __clang__
    using client_word = long;      # unchanged between v1 and v2
    #else
    using client_word = int;       # v1
    using client_word = long long; # v2
    #endif

`lab_consume(client_word)` mangles the width into its exported symbol, so
under GCC the pair is `_Z11lab_consumei` -> `_Z11lab_consumex` (a real
binary break at L0, no headers needed) while under Clang it stays
`_Z11lab_consumel` on both sides.  The expected aggregate is therefore:

    affected_profiles:   [the gcc profile]
    unaffected_profiles: [the clang profile]

A run that reports the same verdict for both producers has lost the
attribution, whichever verdict it is -- so both halves are asserted, not
just the breaking one.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import yaml

from scenario_command import run_command

# Verdicts that mean "this producer's ABI did not move".
UNAFFECTED_VERDICTS = frozenset({"NO_CHANGE", "COMPATIBLE"})


class ScenarioError(RuntimeError):
    """The scenario could not be run as declared."""


def classify_profiles(verdicts: Dict[str, str]) -> Dict[str, List[str]]:
    """Split producer profiles into affected/unaffected by their verdict.

    Deliberately total: every profile lands in exactly one list, so a
    profile whose comparison never ran cannot silently vanish from both.
    """
    affected = sorted(
        profile for profile, verdict in verdicts.items()
        if verdict not in UNAFFECTED_VERDICTS
    )
    unaffected = sorted(
        profile for profile, verdict in verdicts.items()
        if verdict in UNAFFECTED_VERDICTS
    )
    return {"affected_profiles": affected, "unaffected_profiles": unaffected}


def assert_expectation(
    actual: Dict[str, List[str]], expected: Dict[str, Any]
) -> List[str]:
    """Compare the aggregate accounting against the manifest's declaration."""
    errors = []
    for key in ("affected_profiles", "unaffected_profiles"):
        want = sorted(expected.get(key, []))
        got = sorted(actual.get(key, []))
        if got != want:
            errors.append(f"{key}={got!r}, expected {want!r}")
    # A scenario that declares no affected profile, or no unaffected one,
    # is not testing attribution at all -- it would pass just as happily
    # against a scanner that reported one verdict for everything.
    if not expected.get("affected_profiles") or not expected.get("unaffected_profiles"):
        errors.append(
            "expectation must name at least one affected AND one unaffected "
            "profile, or the scenario cannot detect a loss of attribution"
        )
    return errors


def _resolve_compiler(producer: Dict[str, Any], profile_id: str) -> str:
    cxx = producer.get("cxx")
    if not cxx:
        raise ScenarioError(f"{profile_id}: producer declares no cxx executable")
    resolved = shutil.which(cxx)
    if resolved is None:
        # Never skip: a missing declared producer makes the whole aggregate
        # vacuous, and a vacuously green attribution scenario is worse than
        # a red one.
        raise ScenarioError(
            f"{profile_id}: declared producer compiler {cxx!r} is not on PATH; "
            "the attribution aggregate cannot be computed without it"
        )
    return resolved


def run_producer(
    profile_id: str,
    producer: Dict[str, Any],
    fixture: Path,
    output: Path,
) -> str:
    """Build both fixture sides with one producer and return the verdict."""
    cxx = _resolve_compiler(producer, profile_id)
    standard = producer.get("standard", "c++17")
    snapshots = []
    for side in ("v1", "v2"):
        root = fixture / side
        library = output / f"{profile_id}-{side}.so"
        run_command([
            cxx, f"-std={standard}", "-g", "-fPIC", "-shared", f"-I{root}",
            str(root / "api.cc"), "-o", str(library),
        ])
        snapshot = output / f"{profile_id}-{side}.abicheck.json"
        run_command([
            "abicheck", "dump", str(library),
            "--header", str(root / "api.h"),
            "--depth", "binary",
            "-o", str(snapshot),
        ])
        snapshots.append(snapshot)
    report_path = output / f"{profile_id}-report.json"
    run_command(
        ["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
         "--format", "json", "--policy", "strict_abi", "-o", str(report_path)],
        verdict_report=report_path,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("operational_errors"):
        raise ScenarioError(
            f"{profile_id}: comparison reported operational errors: "
            f"{report['operational_errors']!r}"
        )
    verdict = report.get("verdict")
    if not isinstance(verdict, str) or not verdict:
        raise ScenarioError(f"{profile_id}: report carries no verdict")
    return verdict


def run(manifest: Path, scenario_id: str, output: Path) -> List[str]:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    try:
        scenario = next(
            item for item in doc.get("producer_scenarios", [])
            if item.get("id") == scenario_id
        )
    except StopIteration:
        raise ScenarioError(f"{manifest}: no producer scenario {scenario_id!r}") from None
    fixture = manifest.parents[1] / scenario["fixture"]
    producers = scenario.get("producers", {})
    if len(producers) < 2:
        raise ScenarioError(
            f"{scenario_id}: needs at least two producer profiles to compare"
        )
    output.mkdir(parents=True, exist_ok=True)

    verdicts = {
        profile_id: run_producer(profile_id, producer, fixture, output)
        for profile_id, producer in sorted(producers.items())
    }
    actual = classify_profiles(verdicts)
    (output / "attribution.json").write_text(
        json.dumps({"verdicts": verdicts, **actual}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return assert_expectation(actual, scenario.get("expect", {}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--scenario", default="producer-compiler-word-width")
    parser.add_argument("--output", type=Path, default=Path("producer-compiler-results"))
    args = parser.parse_args(argv)
    try:
        errors = run(args.manifest, args.scenario, args.output)
    except (OSError, ScenarioError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
