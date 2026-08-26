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
distinction, whichever verdict it is -- so both halves are asserted, not
just the breaking one.

Scope, stated precisely (Codex review, PR #30). ABICheck is never told
which profile it is looking at here: each producer's pair is dumped and
compared on its own, and this runner attaches the profile id. So what is
proved is that ABICheck's own findings diverge by producer -- the GCC side
must report the exact symbol pair the width change produces, and the Clang
side must report nothing -- which is why the per-producer FINDINGS are
asserted from the manifest, not merely the verdicts. What is NOT proved is
that the native project path routes a per-profile cell's findings to the
right profile: that needs the Clang profile to be a contract profile with
its own accepted-main baseline, which it is not yet, and it is recorded as
the `producer-attribution-through-project-path` expected gap rather than
implied by this scenario's name.
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


def assert_producer_findings(
    report: Dict[str, Any], expected: Dict[str, Any], profile_id: str
) -> List[str]:
    """Assert one producer's own findings, not just its verdict.

    A verdict string alone leaves this runner doing most of the work: it
    would still pass if ABICheck reported the right verdict for the wrong
    reason. The GCC side must actually name the symbol pair the width change
    produces, and the Clang side must name nothing.
    """
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(
            f"{profile_id}: verdict={report.get('verdict')!r}, "
            f"expected {expected['verdict']!r}"
        )
    # Codex review: severity belongs in this identity. ABICheck could
    # downgrade `typedef_base_changed` while `func_removed` still carried the
    # GCC verdict, and a (kind, symbol) comparison would stay green even
    # though the per-producer CLASSIFICATION had moved -- which is what this
    # scenario claims to pin.
    #
    # Severity is compared only where the manifest declares one, and that is
    # NOT the usual "absent reads as OK" hole: observed_findings() records
    # every severity the run actually saw into attribution.json on every run,
    # so the values needed to declare them are produced as evidence rather
    # than guessed. They are not declared yet because this environment has
    # neither g++-14 nor castxml, so the scenario cannot be reproduced
    # faithfully here, and inventing severities would assert a classification
    # nothing verified. A test pins that every declared finding either
    # carries a severity or is listed as awaiting one, so this cannot be
    # quietly forgotten.
    declared_findings = expected.get("findings", [])
    with_severity = [f for f in declared_findings if "severity" in f]
    if with_severity:
        observed = {
            (change.get("kind"), change.get("symbol"), change.get("severity"))
            for change in (report.get("changes") or [])
        }
        wanted = {(f["kind"], f["symbol"], f["severity"]) for f in declared_findings}
        if observed != wanted:
            errors.append(
                f"{profile_id}: findings={sorted(observed)!r}, expected {sorted(wanted)!r}"
            )
        return errors
    observed = {
        (change.get("kind"), change.get("symbol"))
        for change in (report.get("changes") or [])
    }
    declared = {(f["kind"], f["symbol"]) for f in declared_findings}
    if observed != declared:
        errors.append(
            f"{profile_id}: findings={sorted(observed)!r}, expected {sorted(declared)!r}"
        )
    return errors


def observed_findings(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every finding a producer reported, WITH its severity.

    Written into attribution.json so the severities needed to tighten the
    declarations are produced by a real run rather than guessed. The
    scenario cannot be reproduced outside CI (it needs both g++-14 and
    castxml), so this is how the evidence gets out.
    """
    return sorted(
        (
            {
                "kind": change.get("kind"),
                "symbol": change.get("symbol"),
                "severity": change.get("severity"),
            }
            for change in (report.get("changes") or [])
            if isinstance(change, dict)
        ),
        key=lambda f: (str(f["kind"]), str(f["symbol"])),
    )


def run_producer(
    profile_id: str,
    producer: Dict[str, Any],
    fixture: Path,
    output: Path,
) -> Dict[str, Any]:
    """Build both fixture sides with one producer and return its report."""
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
    return report


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

    reports = {
        profile_id: run_producer(profile_id, producer, fixture, output)
        for profile_id, producer in sorted(producers.items())
    }
    verdicts = {pid: report.get("verdict") for pid, report in reports.items()}
    actual = classify_profiles(verdicts)

    expect = scenario.get("expect", {})
    errors = assert_expectation(actual, expect)
    # Per-producer findings, where declared -- this is the half that makes
    # ABICheck's own output the subject rather than this runner's labelling.
    per_producer = expect.get("producers", {})
    if set(per_producer) != set(producers):
        errors.append(
            f"expect.producers covers {sorted(per_producer)!r}, but the scenario "
            f"declares producers {sorted(producers)!r}; every producer must "
            "declare the findings it is expected to produce"
        )
    for profile_id, declared in sorted(per_producer.items()):
        report = reports.get(profile_id)
        if report is None:
            continue
        errors.extend(assert_producer_findings(report, declared, profile_id))

    (output / "attribution.json").write_text(
        json.dumps(
            {
                "verdicts": verdicts,
                # Severities included so tightening the declarations uses real
                # values from a real run rather than invented ones.
                "observed_findings": {
                    profile_id: observed_findings(report)
                    for profile_id, report in sorted(reports.items())
                },
                **actual,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return errors


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
