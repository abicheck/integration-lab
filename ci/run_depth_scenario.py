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
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")
    actual = {(item.get("kind"), item.get("symbol")) for item in report.get("changes", [])}
    wanted = {(item["kind"], item["symbol"]) for item in expected.get("findings", [])}
    if actual != wanted:
        errors.append(f"findings={sorted(actual)!r}, expected {sorted(wanted)!r}")
    if report.get("operational_errors"):
        errors.append("report contains operational errors")
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
        run_command(["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
                     "--format", "json", "--policy", "strict_abi", "-o", str(report_path)],
                    verdict_report=report_path)
        report = json.loads(report_path.read_text())
        errors.extend(f"{depth}: {error}" for error in _assert_report(report, scenario["expect"][depth]))
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
