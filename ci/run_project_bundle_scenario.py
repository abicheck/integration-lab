#!/usr/bin/env python3
"""Build and compare a real provider/consumer DSO bundle."""
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
    wanted = expected["bundle_finding"]
    matches = [finding for finding in report.get("bundle_findings", [])
               if all(finding.get(key) == value for key, value in wanted.items())]
    if len(matches) != 1:
        errors.append(f"expected exactly one matching cross-DSO finding, got {len(matches)}")
    libraries = {item.get("library"): item.get("verdict") for item in report.get("libraries", [])}
    if libraries.get("libmath.so") != "NO_CHANGE":
        errors.append(f"consumer cell should stay NO_CHANGE, got {libraries.get('libmath.so')!r}")
    if libraries.get("libcore.so") != "BREAKING":
        errors.append(f"provider cell should be BREAKING, got {libraries.get('libcore.so')!r}")
    return errors


def run(manifest: Path, output: Path, cc: str) -> list[str]:
    doc = yaml.safe_load(manifest.read_text()) or {}
    scenario = next(item for item in doc["project_scenarios"] if item["id"] == "project-cross-dso-break")
    root = manifest.parents[1] / scenario["fixture"]
    for side in ("old", "new"):
        directory = output / side
        directory.mkdir(parents=True, exist_ok=True)
        _run([cc, "-shared", "-fPIC", "-g", str(root / side / "core.c"),
              "-Wl,-soname,libcore.so", "-o", str(directory / "libcore.so")])
        _run([cc, "-shared", "-fPIC", "-g", str(root / "math.c"), f"-L{directory}",
              "-lcore", "-Wl,-rpath,$ORIGIN", "-Wl,-soname,libmath.so",
              "-o", str(directory / "libmath.so")])
    report_path = output / "report.json"
    _run(["abicheck", "compare", str(output / "old"), str(output / "new"),
          "--format", "json", "-o", str(report_path)], allow_verdict=True)
    return _oracle(json.loads(report_path.read_text()), scenario["expect"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("project-bundle-results"))
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
