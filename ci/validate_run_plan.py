#!/usr/bin/env python3
"""Assert the native shadow plan contains every declared profile/target cell."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import yaml


def validate(config_path: Path, plan_path: Path) -> list[str]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    profiles = set(config.get("profiles", {}))
    checked_targets = {
        name for name, target in config.get("targets", {}).items() if target.get("checks")
    }
    expected = {
        f"{target}@{profile}#accepted-main@binary"
        for target in checked_targets for profile in profiles
    }
    expected.update(
        f"{name}@{profile}#accepted-main@binary"
        for name in config.get("bundles", {}) for profile in profiles
    )
    checks = plan.get("checks", [])
    actual = {check.get("check_id") for check in checks}
    errors = []
    if actual != expected:
        errors.append(f"run-plan cells differ: actual={sorted(actual)} expected={sorted(expected)}")
    for check in checks:
        if check.get("required") is not True or check.get("gate_mode") != "deferred":
            errors.append(f"{check.get('check_id')}: project cell must be required/deferred")
        if check.get("requested_depth") != "binary":
            errors.append(f"{check.get('check_id')}: expected requested_depth=binary")
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
