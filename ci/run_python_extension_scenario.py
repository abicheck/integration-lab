#!/usr/bin/env python3
"""Build one pybind11 extension and prove a stub-only Python API break."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sysconfig
from pathlib import Path
import yaml


def _run(argv: list[str], allow_verdict: bool = False) -> None:
    result = subprocess.run(argv)
    if result.returncode and not allow_verdict:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}")


def _oracle(report: dict, expected: dict, hashes: list[str]) -> list[str]:
    errors = []
    if expected.get("binary_identical") and len(set(hashes)) != 1:
        errors.append("extension binaries are not byte-identical")
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")
    actual = {(change.get("kind"), change.get("symbol")) for change in report.get("changes", [])}
    wanted = {(change["kind"], change["symbol"]) for change in expected["findings"]}
    if actual != wanted:
        errors.append(f"findings={sorted(actual)!r}, expected {sorted(wanted)!r}")
    return errors


def run(manifest: Path, output: Path, cxx: str) -> list[str]:
    doc = yaml.safe_load(manifest.read_text()) or {}
    scenario = next(item for item in doc["python_scenarios"] if item["id"] == "pybind-keyword-renamed")
    root = manifest.parents[1]
    source, stubs = root / scenario["source"], root / scenario["stubs"]
    output.mkdir(parents=True, exist_ok=True)
    includes = subprocess.check_output(["python", "-m", "pybind11", "--includes"], text=True).split()
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    built = output / f"_core{suffix}"
    _run([cxx, "-O2", "-shared", "-std=c++17", "-fPIC", *includes, str(source), "-o", str(built)])
    snapshots, hashes = [], []
    for side in ("v1", "v2"):
        side_dir = output / side
        side_dir.mkdir()
        binary = side_dir / built.name
        shutil.copy2(built, binary)
        shutil.copy2(stubs / f"{side}.pyi", side_dir / "_core.pyi")
        hashes.append(hashlib.sha256(binary.read_bytes()).hexdigest())
        snapshot = output / f"{side}.json"
        _run(["abicheck", "dump", str(binary), "-o", str(snapshot)])
        snapshots.append(snapshot)
    report_path = output / "report.json"
    _run(["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
          "--format", "json", "--policy", "strict_abi", "-o", str(report_path)], True)
    return _oracle(json.loads(report_path.read_text()), scenario["expect"], hashes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("python-extension-results"))
    parser.add_argument("--cxx", default="c++")
    args = parser.parse_args()
    try:
        errors = run(args.manifest, args.output, args.cxx)
    except (OSError, RuntimeError, StopIteration, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
