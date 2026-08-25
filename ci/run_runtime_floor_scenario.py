#!/usr/bin/env python3
"""Build a deterministic versioned runtime and verify floor modulation."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
import yaml


def _run(argv: list[str], allow_verdict: bool = False) -> None:
    result = subprocess.run(argv)
    if result.returncode and not allow_verdict:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}")


def _oracle(report: dict, expected: dict) -> list[str]:
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")
    floors = [change for change in report.get("changes", [])
              if change.get("kind") == "runtime_floor_raised" and change.get("symbol") == "liblabrt.so:LABRT"]
    if len(floors) != 1:
        errors.append(f"expected exactly one LABRT runtime_floor_raised finding, got {len(floors)}")
    elif floors[0].get("severity") != expected["floor_severity"]:
        errors.append(f"floor severity={floors[0].get('severity')!r}, expected {expected['floor_severity']!r}")
    expected_changes = {
        ("symbol_version_required_added", "LABRT_2.0", expected["floor_severity"]),
        ("symbol_version_required_removed", "LABRT_1.0", "compatible"),
        ("runtime_floor_raised", "liblabrt.so:LABRT", expected["floor_severity"]),
        ("imported_symbol_added", "labrt_new_api", "risk"),
        ("imported_symbol_removed", "labrt_api", "compatible"),
    }
    actual_changes = {
        (change.get("kind"), change.get("symbol"), change.get("severity"))
        for change in report.get("changes", [])
    }
    if actual_changes != expected_changes:
        errors.append(
            f"changes={sorted(actual_changes)!r}, expected exactly {sorted(expected_changes)!r}"
        )
    return errors


def run(manifest: Path, output: Path, cc: str) -> list[str]:
    doc = yaml.safe_load(manifest.read_text()) or {}
    scenario = next(item for item in doc["runtime_scenarios"] if item["id"] == "runtime-floor-raised")
    fixture = manifest.parents[1] / scenario["fixture"]
    output.mkdir(parents=True, exist_ok=True)
    provider = output / "liblabrt.so"
    _run([cc, "-shared", "-fPIC", str(fixture / "provider.c"),
          f"-Wl,--version-script={fixture / 'versions.map'}", "-Wl,-soname,liblabrt.so", "-o", str(provider)])
    snapshots = []
    for side in ("v1", "v2"):
        binary, snapshot = output / f"{side}.so", output / f"{side}.json"
        _run([cc, "-shared", "-fPIC", str(fixture / f"{side}.c"), f"-L{output}", "-llabrt",
              "-Wl,-rpath,$ORIGIN", "-o", str(binary)])
        _run(["abicheck", "dump", str(binary), "-o", str(snapshot)])
        snapshots.append(snapshot)
    errors = []
    matrices = {
        "no_matrix": None,
        "labrt_1": manifest.parent / "env/labrt-1.yaml",
        "labrt_2": manifest.parent / "env/labrt-2.yaml",
    }
    for name, matrix in matrices.items():
        report_path = output / f"report-{name}.json"
        command = ["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
                   "--format", "json", "--policy", "strict_abi", "-o", str(report_path)]
        if matrix:
            command += ["--env-matrix", str(matrix)]
        _run(command, allow_verdict=True)
        errors.extend(f"{name}: {error}" for error in _oracle(
            json.loads(report_path.read_text()), scenario["expect"][name]))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("runtime-floor-results"))
    parser.add_argument("--cc", default="gcc")
    args = parser.parse_args()
    try:
        errors = run(args.manifest, args.output, args.cc)
    except (OSError, RuntimeError, StopIteration) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
