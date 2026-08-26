#!/usr/bin/env python3
"""Run the deterministic L2-green/L4-red macro scenario with real ABICheck."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
import yaml

from scenario_command import run_command
from validate_source_depth import validate as validate_depth


def _assert_report(report: dict, expected: dict, requested_depth: str) -> list[str]:
    """Assert the verdict, the findings, AND the assurance behind them.

    Matching the expected verdict is not enough.  An L4 run that silently
    degraded to L2 evidence can still land on the right answer for one
    particular mutation while proving nothing about source depth in general
    -- that is exactly the failure this scenario exists to catch, so the
    compare-level `analysis_assurance` block is gated here, not just the
    per-side dump provenance.
    """
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")
    actual = {(item.get("kind"), item.get("symbol")) for item in report.get("changes", [])}
    wanted = {(item["kind"], item["symbol"]) for item in expected.get("findings", [])}
    if actual != wanted:
        errors.append(f"findings={sorted(actual)!r}, expected {sorted(wanted)!r}")

    # requested_depth/effective_depth/depth_satisfied/status, plus
    # operational_errors.  These fixtures are built from one tree in one
    # job, so `complete` is achievable and anything less is a real gap.
    errors.extend(validate_depth(report, requested_depth, require_complete=True))

    assurance = report.get("analysis_assurance")
    if isinstance(assurance, dict):
        effective = assurance.get("effective_depth")
        if effective != expected["effective_depth"]:
            errors.append(
                f"compare analysis_assurance.effective_depth={effective!r}, "
                f"expected {expected['effective_depth']!r}"
            )
    operational = report.get("operational_errors")
    if operational:
        errors.append(f"report contains {len(operational)} operational error(s): {operational!r}")
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
            if provenance.get("effective_depth") != scenario["expect"][depth]["effective_depth"]:
                errors.append(f"{side}/{depth}: effective depth mismatch")
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
            for error in _assert_report(report, scenario["expect"][depth], depth)
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
