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


def normalized_suppressed(report: Dict[str, Any]) -> Set[Tuple[str, str, str]]:
    """Findings a suppression rule accepted -- also semantic, not provenance.

    ABICheck nests these under `report["suppression"]["suppressed_changes"]`,
    which is where scripts/run_scenario.py's own oracle reads them. A
    top-level lookup returns nothing for every real report, so this
    assertion silently compared two empty sets and could never have failed
    -- dead code wearing the shape of a check (Codex review, PR #30). The
    top-level fallback is kept only for hand-written fixtures in tests.
    """
    block = report.get("suppression")
    changes = None
    if isinstance(block, dict):
        changes = block.get("suppressed_changes")
    if changes is None:
        changes = report.get("suppressed_changes")
    # Codex review: this projected to the SYMBOL alone, while unsuppressed
    # findings are compared on (kind, symbol, severity). Two build systems
    # suppressing different kinds -- or the same kind at different severity --
    # on the same symbol therefore produced equal suppression sets, and the
    # per-run oracle checks only count and symbols, so a real semantic
    # disagreement about WHAT was suppressed passed both gates. Suppressed and
    # unsuppressed findings are the same objects on either side of one rule;
    # comparing them by different identities was the bug.
    return {
        (str(change.get("kind")), str(change.get("symbol")), str(change.get("severity")))
        for change in changes or []
        if isinstance(change, dict)
    }


def operational_errors(report: Dict[str, Any]) -> List[Any]:
    """Every operational-error channel a scenario report can carry.

    A report carrying operational errors is not comparable evidence, even
    when its verdict and findings match: the run that produced it was
    incomplete. That matters most for scenarios whose expected finding set
    is EMPTY, where an incomplete run and a clean run look identical
    (Codex review, PR #30).
    """
    found = list(report.get("operational_errors") or [])
    for library in report.get("libraries") or []:
        if isinstance(library, dict):
            found.extend(library.get("operational_errors") or [])
    return found


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


def load_declared(path: Path) -> Dict[str, Set[str]]:
    """{build system: declared scenario names} from scenarios/build-matrix.yaml."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - CI always has it
        raise ParityError(f"PyYAML is required to read {path}: {exc}") from exc
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ParityError(f"{path}: unreadable build matrix: {exc}") from exc
    systems = document.get("build_systems")
    if not isinstance(systems, dict) or not systems:
        raise ParityError(f"{path}: no build_systems declared")
    return {name: set(mapping or {}) for name, mapping in systems.items()}


#: The build system these fixtures are shaped to MATCH. buildsystems/*/fixtures
#: exist to reproduce the Bazel path's artifact shape -- the Makefile and the
#: CMakeLists both suppress a SONAME for exactly that reason -- so a parity run
#: without Bazel is not comparing against the reference, it is comparing two
#: imitations with each other.
#:
#: Codex review: build-matrix.yaml declares only cmake and make (Bazel's recipe
#: lives in the semantic manifest, deliberately), so `missing_declared_reports`
#: could never require Bazel, and dropping or mistyping `bazel=parity/bazel` in
#: the workflow left cmake+make satisfying both that check and compare()'s
#: two-system minimum. The job stayed green having lost its reference leg.
REFERENCE_BUILD_SYSTEM = "bazel"


def missing_reference_leg(
    results: Dict[str, Dict[str, Dict[str, Any]]],
    declared: Dict[str, Set[str]] | None = None,
    profiles: Dict[str, Set[str]] | None = None,
    *,
    allow_partial: bool = False,
) -> List[str]:
    """The reference build system must cover every cell that gets compared.

    Codex review, third pass (PR #30): the previous version accepted the mere
    PRESENCE of a `bazel` key. That is the same "green but proves nothing"
    shape one level down. `missing_declared_reports` derives its cells from
    build-matrix.yaml, which declares only cmake and make, so it never
    required a single Bazel report; and `compare()` picks its per-cell
    reference with `sorted(present)[0]`, which silently promotes `cmake` the
    moment a Bazel cell is absent. A `bazel` results directory that was empty
    -- or merely missing `add_function.clang` -- therefore satisfied this
    check while that cell went on being "parity-checked" between two
    imitations of the very build system that was not there.

    So the requirement is per-CELL, not per-directory: every cell any build
    system declares must also have been reported by the reference.
    """
    if allow_partial:
        return []
    reference = results.get(REFERENCE_BUILD_SYSTEM)
    if reference is None:
        return [
            f"{REFERENCE_BUILD_SYSTEM}: the reference build system supplied no results "
            f"-- got {sorted(results)}. These fixtures are shaped to match it, so a "
            "comparison without it compares two imitations with each other "
            "(pass --allow-partial for a deliberate partial run)"
        ]
    if declared is None:
        return []
    profiles = profiles or {}
    required: Set[str] = set()
    for scenarios in declared.values():
        for scenario in scenarios:
            required |= expected_stems(scenario, profiles.get(scenario, set()))
    return [
        f"{stem}: declared as a cross-build cell but the reference build system "
        f"({REFERENCE_BUILD_SYSTEM}) produced no report for it -- that cell would be "
        "compared between two imitations with no reference evidence"
        for stem in sorted(required - set(reference))
    ]


def scenario_profiles(path: Path) -> Dict[str, Set[str]]:
    """{scenario name: declared profile ids} from scenarios/manifest.yaml.

    A scenario declares `expected: {castxml: V, clang: V}` (one run per named
    header frontend, and every scenario in the suite uses this form) or the
    older `expected_verdict: V` (a single run under abicheck's default
    frontend, whose report carries no profile suffix). An empty set means the
    latter.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - CI always has it
        raise ParityError(f"PyYAML is required to read {path}: {exc}") from exc
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ParityError(f"{path}: unreadable scenario manifest: {exc}") from exc
    entries = document.get("scenarios")
    if not isinstance(entries, list) or not entries:
        raise ParityError(f"{path}: no scenarios declared")
    profiles: Dict[str, Set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        expected = entry.get("expected")
        profiles[str(entry["name"])] = (
            {str(key) for key in expected} if isinstance(expected, dict) and expected else set()
        )
    return profiles


def expected_stems(scenario: str, profiles: Set[str]) -> Set[str]:
    """The report stems a scenario must produce under one build system."""
    return {f"{scenario}.{profile}" for profile in profiles} or {scenario}


def missing_declared_reports(
    results: Dict[str, Dict[str, Dict[str, Any]]],
    declared: Dict[str, Set[str]],
    profiles: Dict[str, Set[str]],
    *,
    allow_partial: bool = False,
) -> List[str]:
    """Every declared (build system, scenario, profile) cell must have reported.

    run_scenario.py treats an absent mapping as a SKIP, not a failure, so
    deleting a CMake or Make mapping silently removes that scenario from
    the comparison. Comparing "whichever reports happen to exist" would
    then stay green while coverage shrank underneath it (Codex review,
    PR #30). Checking against the declaration makes shrinking coverage a
    red gate rather than an invisible one.

    Codex review, second pass: the first version of this check collapsed a
    report stem at its first dot, so `add_function.castxml` alone satisfied
    the requirement for `add_function` and a missing `add_function.clang`
    passed -- while compare() went on comparing the Clang reports that DID
    survive on other build systems. One build-system/frontend cell could
    vanish with the parity gate still green, which is the exact hole this
    check exists to close, one level finer. The unit is the CELL, so the
    full stem is what must be present.
    """
    errors = []
    for build_system, scenarios in sorted(declared.items()):
        reports = results.get(build_system)
        if reports is None:
            # Codex review, second pass: this used to `continue`, on the
            # reasoning that a build system absent from --results was the
            # caller's deliberate choice. That is true for a local two-way
            # run and false for the case that matters -- dropping the Make
            # pair from the workflow's --results, or mistyping its name,
            # silently removed an entire leg while Bazel and CMake still
            # compared clean and the gate stayed green. Exempting a whole
            # missing leg is a strictly bigger hole than the missing cells
            # this function exists to catch.
            #
            # Default is now strict, so losing a leg is loud. The deliberate
            # partial run keeps its escape hatch, but has to ask for it by
            # name (--allow-partial), because the dangerous configuration
            # should be the one that takes an explicit act.
            if allow_partial:
                continue
            errors.append(
                f"{build_system}: declared in the build matrix but no results were "
                "supplied -- an entire build system is missing from the comparison "
                "(pass --allow-partial for a deliberate partial run)"
            )
            continue
        for scenario in sorted(scenarios):
            for stem in sorted(expected_stems(scenario, profiles.get(scenario, set()))):
                if stem not in reports:
                    errors.append(
                        f"{stem}: declared for {build_system} in the build matrix "
                        "but produced no report -- coverage shrank silently"
                    )
    return errors


def scenario_gaps(path: Path) -> Dict[str, Dict[str, Any]]:
    """{scenario name: its `expected_gap` block} from scenarios/manifest.yaml."""
    import yaml
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ParityError(f"{path}: unreadable scenario manifest: {exc}") from exc
    return {
        str(entry["name"]): entry["expected_gap"]
        for entry in document.get("scenarios") or []
        if isinstance(entry, dict) and entry.get("name") and entry.get("expected_gap")
    }


def known_divergences(path: Path) -> List[Dict[str, Any]]:
    """`parity_known_divergences` from scenarios/manifest.yaml, validated."""
    import yaml
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ParityError(f"{path}: unreadable scenario manifest: {exc}") from exc
    out = []
    for index, entry in enumerate(document.get("parity_known_divergences") or []):
        findings = entry.get("findings") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not entry.get("id")
            or not entry.get("upstream_issue")
            or not entry.get("build_system")
            or not isinstance(findings, list)
            or not findings
            or not all(isinstance(f, list) and len(f) == 3 for f in findings)
        ):
            raise ParityError(
                f"{path}: parity_known_divergences[{index}] needs id, upstream_issue, "
                "build_system and a non-empty list of [kind, symbol, severity] findings"
            )
        out.append({**entry, "findings": {tuple(str(x) for x in f) for f in findings}})
    return out


def apply_known_divergences(
    stem: str, build_system: str, report: Dict[str, Any],
    divergences: List[Dict[str, Any]], applied: Dict[str, int],
) -> Tuple[Dict[str, Any], List[str]]:
    """Remove a declared, build-system-specific upstream divergence -- all or
    nothing. A report carrying ALL of a divergence's exact (kind, symbol,
    severity) findings has exactly those removed; one carrying only SOME of
    them is an error (the divergence changed shape). Anything else in the
    report is untouched, so a real divergence outside the declaration still
    fails. `applied` counts uses so a declaration that never applies can be
    reported as closed by the caller."""
    errors: List[str] = []
    for divergence in divergences:
        if divergence["build_system"] != build_system:
            continue
        present = normalized_findings(report) & divergence["findings"]
        if not present:
            continue
        if present != divergence["findings"]:
            errors.append(
                f"{stem}: {build_system} carries only part of known divergence "
                f"{divergence['id']} ({sorted(present)!r}) -- its shape changed"
            )
            continue
        applied[divergence["id"]] = applied.get(divergence["id"], 0) + 1
        report = dict(report)
        report["changes"] = [
            c for c in report.get("changes") or []
            if not (
                isinstance(c, dict)
                and (str(c.get("kind")), str(c.get("symbol")), str(c.get("severity"))) in divergence["findings"]
            )
        ]
    return report, errors


def effective_report(
    stem: str, build_system: str, report: Dict[str, Any], gaps: Dict[str, Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[str]]:
    """The report parity should compare for one (cell, build system).

    A build system with a declared `expected_gap.observed.<build system>`
    entry is compared through that gap, never around it: the raw report must
    match the declared outcome EXACTLY (the same matcher run_scenario.py
    uses), and only then is the entry's declared `parity` projection applied
    -- `drop_findings` (each must actually be present) removed and `verdict`
    substituted -- yielding the build-system-neutral result. A declared gap
    that does not match is an error, not a silent fallback to the raw report.
    A build system without an entry is compared raw, exactly as before. A
    gap that has CLOSED is caught by run_scenario.py's own per-build-system
    leg (it fails "closed"); here it shows up as a mismatch.
    """
    from run_scenario import evaluate_expected_gap  # shared matcher, no copy

    scenario = stem.split(".", 1)[0]
    gap = gaps.get(scenario)
    observed = ((gap or {}).get("observed") or {}).get(build_system)
    if not observed:
        return report, []
    findings = sorted(
        f"{c.get('kind')}:{c.get('symbol')}" for c in report.get("changes") or [] if isinstance(c, dict)
    )
    status, detail = evaluate_expected_gap(
        gap, build_system=build_system, oracle_passed=False,
        actual_verdict=report.get("verdict"), actual_findings=findings,
        suppression_passed=True,
    )
    if status != "matched":
        return report, [f"{stem}: {build_system} declared expected_gap does not match: {detail}"]
    projection = observed.get("parity")
    if not isinstance(projection, dict) or "verdict" not in projection:
        return report, [
            f"{stem}: {build_system} expected_gap has no parity projection "
            "(observed.<bs>.parity.verdict/drop_findings) -- cannot compare it to other build systems"
        ]
    drop = set(projection.get("drop_findings") or [])
    missing = sorted(drop - set(findings))
    if missing:
        return report, [f"{stem}: {build_system} parity.drop_findings not present in the report: {missing}"]
    effective = dict(report)
    effective["verdict"] = projection["verdict"]
    effective["changes"] = [
        c for c in report.get("changes") or []
        if not (isinstance(c, dict) and f"{c.get('kind')}:{c.get('symbol')}" in drop)
    ]
    return effective, []


def compare(
    results: Dict[str, Dict[str, Dict[str, Any]]],
    gaps: Dict[str, Dict[str, Any]] | None = None,
    divergences: List[Dict[str, Any]] | None = None,
) -> List[str]:
    """Compare every scenario across the build systems that produced it."""
    errors: List[str] = []
    gaps = gaps or {}
    divergences = divergences or []
    applied: Dict[str, int] = {}
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
        # Comparability first: a report with operational errors is not
        # evidence, so comparing it to another would be comparing noise.
        incomplete = False
        for build_system, report in sorted(present.items()):
            problems = operational_errors(report)
            if problems:
                incomplete = True
                errors.append(
                    f"{scenario}: {build_system} report carries "
                    f"{len(problems)} operational error(s): {problems!r}"
                )
        if incomplete:
            compared += 1
            continue

        compared += 1
        projected = {}
        for build_system, report in present.items():
            projected[build_system], gap_errors = effective_report(scenario, build_system, report, gaps)
            errors.extend(gap_errors)
            projected[build_system], div_errors = apply_known_divergences(
                scenario, build_system, projected[build_system], divergences, applied
            )
            errors.extend(div_errors)
        present = projected
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
    for divergence in divergences:
        if divergence["build_system"] in results and not applied.get(divergence["id"]):
            errors.append(
                f"known divergence {divergence['id']} ({divergence['upstream_issue']}) was not "
                f"observed on {divergence['build_system']} in any compared report -- it has "
                "closed; remove the parity_known_divergences entry"
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
    parser.add_argument(
        "--build-matrix", type=Path, default=Path("scenarios/build-matrix.yaml"),
        help="the declared matrix; every build system that DECLARES a scenario "
             "must have produced a report for it",
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("scenarios/manifest.yaml"),
        help="the semantic manifest; supplies each scenario's declared header "
             "frontends, so a missing build-system/frontend CELL is caught "
             "rather than only a missing scenario",
    )
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="permit a declared build system to be absent from --results. For "
             "deliberate partial local runs only: in CI this is what makes a "
             "dropped or mistyped leg loud instead of invisible.",
    )
    parser.add_argument("--out", type=Path, help="write a JSON parity receipt here")
    args = parser.parse_args(argv)
    try:
        directories = _parse_results(args.results)
        results = {name: load_results(path) for name, path in directories.items()}
        declared = load_declared(args.build_matrix)
        profiles = scenario_profiles(args.manifest)
        gaps = scenario_gaps(args.manifest)
        divergences = known_divergences(args.manifest)
    except ParityError as exc:
        print(f"ERROR: {exc}")
        return 1

    errors = (
        missing_declared_reports(
            results, declared, profiles, allow_partial=args.allow_partial
        )
        + missing_reference_leg(
            results, declared, profiles, allow_partial=args.allow_partial
        )
        + compare(results, gaps, divergences)
    )
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
