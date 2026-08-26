#!/usr/bin/env python3
"""Run the deterministic L2-green/L4-red macro scenario with real ABICheck."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
import yaml

from scenario_command import run_command


def _assert_report(report: dict, expected: dict) -> list[str]:
    """Assert the verdict, the findings, AND the assurance behind them.

    Matching the expected verdict is not enough: an L4 run that silently
    degraded to weaker evidence can still land on the right answer for one
    particular mutation while proving nothing about source depth in general.

    Which field proves that is not obvious, and was got wrong before this
    comment existed -- so, from the real reports this scenario produces:

    * The compare report's ``analysis_assurance.requested_depth`` and
      ``depth_satisfied`` are always ``None``. ``abicheck compare`` between
      two snapshots is never given a ``--depth``; the depth was requested at
      ``dump`` time. Asserting them here only ever fails.
    * The compare report's ``effective_depth`` reads ``source`` for BOTH
      stages, because it reflects the richest evidence present in the
      snapshots (these fixtures are built with ``-g``, so DWARF and headers
      are always there) rather than the depth that was asked for. It cannot
      detect a fallback either.
    * ``dump_provenance.requested_depth``/``effective_depth`` on each
      snapshot is where a fallback actually shows, and run() gates both.
    * On the compare side, ``fact_set_comparability`` and
      ``l3_context_status`` are what distinguish a comparison that really
      used L3/L4 evidence (``comparable``/``clean``) from one that did not
      (``not_applicable``/``not_evaluated``) -- so those are asserted here,
      per stage, from the manifest.

    ``status: complete`` is required only where the manifest says so. The
    headers stage legitimately reports ``partial`` for an orthogonal channel
    (``--scope-public-headers`` cannot resolve a public surface for this
    fixture), which has nothing to do with depth; failing on it would be
    noise, and papering over it by loosening the source stage too would give
    up the check that matters.
    """
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")
    actual = {(item.get("kind"), item.get("symbol")) for item in report.get("changes", [])}
    wanted = {(item["kind"], item["symbol"]) for item in expected.get("findings", [])}
    if actual != wanted:
        errors.append(f"findings={sorted(actual)!r}, expected {sorted(wanted)!r}")

    operational = report.get("operational_errors")
    if operational:
        errors.append(f"report contains {len(operational)} operational error(s): {operational!r}")

    compare = expected.get("compare_assurance")
    if not compare:
        return errors
    assurance = report.get("analysis_assurance")
    if not isinstance(assurance, dict):
        errors.append("analysis_assurance is missing; the comparison is unproven")
        return errors
    for field, want in sorted(compare.items()):
        got = assurance.get(field)
        if got != want:
            notes = assurance.get("notes") or []
            detail = f" ({'; '.join(str(note) for note in notes)})" if notes else ""
            errors.append(
                f"analysis_assurance.{field}={got!r}, expected {want!r}{detail}"
            )
    return errors


def run(manifest: Path, scenario_id: str, output: Path, cxx: str) -> list[str]:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    scenario = next(item for item in doc.get("depth_scenarios", []) if item["id"] == scenario_id)
    fixture = manifest.parents[1] / scenario["fixture"]
    output.mkdir(parents=True, exist_ok=True)
    errors = []

    for side in ("v1", "v2"):
        root = fixture / side
        library = output / f"{side}.so"
        run_command([cxx, "-std=c++17", "-g", "-fPIC", "-shared", f"-I{root}",
              str(root / "api.cc"), "-o", str(library)])
        compile_db = [{
            "directory": str(root.resolve()),
            "command": f"{cxx} -std=c++17 -fPIC -I{root.resolve()} -c {root.resolve() / 'api.cc'} -o api.o",
            "file": str((root / "api.cc").resolve()),
        }]
        (output / f"{side}-compile_commands.json").write_text(json.dumps(compile_db))

    for depth in ("headers", "source"):
        snapshots = []
        for side in ("v1", "v2"):
            root = fixture / side
            snapshot = output / f"{side}-{depth}.json"
            command = ["abicheck", "dump", str(output / f"{side}.so"),
                       "--header", str(root / "api.h"), "--depth", depth,
                       "--ast-frontend", "clang", "-o", str(snapshot)]
            if depth == "source":
                command += ["--sources", str(root), "--build-info",
                            str(output / f"{side}-compile_commands.json")]
            run_command(command)
            provenance = json.loads(snapshot.read_text()).get("dump_provenance", {})
            # Both halves: requested proves the scenario asked for the depth
            # it claims to test, effective proves the scanner delivered it.
            # An L4 run that silently degraded to L2 shows up here and
            # nowhere else (see _assert_report()'s docstring).
            if provenance.get("requested_depth") != depth:
                errors.append(
                    f"{side}/{depth}: dump requested_depth="
                    f"{provenance.get('requested_depth')!r}, expected {depth!r}"
                )
            expected_effective = scenario["expect"][depth]["effective_depth"]
            if provenance.get("effective_depth") != expected_effective:
                errors.append(
                    f"{side}/{depth}: dump effective_depth="
                    f"{provenance.get('effective_depth')!r}, expected {expected_effective!r}"
                )
            snapshots.append(snapshot)
        report_path = output / f"report-{depth}.json"
        compare_command = ["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
                           "--format", "json", "--policy", "strict_abi",
                           "-o", str(report_path)]
        if depth == "source":
            # Asks the scanner itself to treat incomplete assurance as a
            # failure.  Note the exit contribution is folded with max, so a
            # BREAKING/API_BREAK verdict's own 2/4 masks the 1 -- the flag
            # documents intent and catches the clean-verdict case, while
            # _assert_report()'s require_complete check is what actually
            # gates this scenario's API_BREAK stage.
            compare_command.append("--require-complete-analysis")
        run_command(compare_command, verdict_report=report_path)
        report = json.loads(report_path.read_text())
        errors.extend(
            f"{depth}: {error}"
            for error in _assert_report(report, scenario["expect"][depth])
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--scenario", default="l2-green-l4-macro-break")
    parser.add_argument("--output", type=Path, default=Path("depth-scenario-results"))
    parser.add_argument("--cxx", default=shutil.which("g++") or "g++")
    args = parser.parse_args()
    try:
        errors = run(args.manifest, args.scenario, args.output, args.cxx)
    except (OSError, RuntimeError, StopIteration) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
