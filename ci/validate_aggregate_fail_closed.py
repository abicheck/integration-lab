#!/usr/bin/env python3
"""Prove the native aggregate rejects a missing required report."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def validate_result(returncode: int, aggregate: dict, expected_count: int) -> list[str]:
    errors = []
    if returncode == 0:
        errors.append("aggregate unexpectedly passed with no reports")
    missing = aggregate.get("coverage", {}).get("missing_required_targets", [])
    if len(missing) != expected_count:
        errors.append(f"missing_required count={len(missing)}, expected {expected_count}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-plan", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.run_plan.read_text())
    expected_count = sum(1 for check in plan.get("checks", []) if check.get("required", True))
    with tempfile.TemporaryDirectory() as reports_dir:
        output = Path(reports_dir) / "aggregate.json"
        result = subprocess.run([
            "abicheck", "aggregate", reports_dir, "--run-plan", str(args.run_plan),
            "--format", "json", "--output", str(output),
        ])
        if not output.is_file():
            print("ERROR: aggregate did not write its result")
            return 1
        errors = validate_result(result.returncode, json.loads(output.read_text()), expected_count)
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
