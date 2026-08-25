#!/usr/bin/env python3
"""Assert the native run plan exactly realizes the project check topology."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import yaml


def validate(config_path: Path, plan_path: Path) -> list[str]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    all_profiles = set(config.get("profiles", {}))
    expected: dict[str, dict] = {}
    for section in ("targets", "bundles"):
        for name, item in config.get(section, {}).items():
            for check in item.get("checks", []):
                profiles = set(check.get("profiles") or all_profiles)
                channel = check.get("channel")
                depth = check.get("depth")
                for profile in profiles:
                    check_id = f"{name}@{profile}#{channel}@{depth}"
                    expected[check_id] = check
    checks = plan.get("checks", [])
    actual = {check.get("check_id") for check in checks}
    errors = []
    if actual != set(expected):
        errors.append(
            f"run-plan cells differ: actual={sorted(actual)} expected={sorted(expected)}"
        )
    for check in checks:
        check_id = check.get("check_id")
        declaration = expected.get(check_id)
        if declaration is None:
            continue
        required = declaration.get("required", False)
        gate_mode = declaration.get("gate_mode", "immediate")
        depth = declaration.get("depth")
        if check.get("required") is not required:
            errors.append(
                f"{check_id}: required={check.get('required')!r}, expected {required!r}"
            )
        if check.get("gate_mode") != gate_mode:
            errors.append(
                f"{check_id}: gate_mode={check.get('gate_mode')!r}, expected {gate_mode!r}"
            )
        if check.get("requested_depth") != depth:
            errors.append(
                f"{check_id}: requested_depth={check.get('requested_depth')!r}, "
                f"expected {depth!r}"
            )
    gate = plan.get("gate", {})
    if gate.get("missing_required") != "fail" or gate.get("unexpected_target") != "fail":
        errors.append("run-plan did not preserve fail-closed aggregate policy")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-plan", type=Path, required=True)
    args = parser.parse_args()
    errors = validate(args.config, args.run_plan)
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
