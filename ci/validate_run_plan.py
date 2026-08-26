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
    errors = []
    if not isinstance(checks, list):
        return [f"run-plan checks must be a list, got {type(checks).__name__}"]

    # Shape before identity: a non-object cell has no check_id to collect,
    # and folding it into the comparison sets would let a malformed plan
    # read as a merely-different one.  Malformed cells are rejected here and
    # never contribute an identifier to `actual`.
    well_formed = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            errors.append(
                f"run-plan check {index} is not an object (got {type(check).__name__})"
            )
            continue
        check_id = check.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            errors.append(
                f"run-plan check {index} has an invalid check_id: {check_id!r}"
            )
            continue
        well_formed.append((check_id, check))

    # A duplicate check_id is not a cosmetic problem: aggregate projects the
    # run plan to an expected-target set keyed by check_id, so two cells
    # sharing one id collapse to a single expectation and one of them can go
    # missing without the gate noticing.  Set comparison alone cannot see it.
    seen = set()
    duplicates = []
    for check_id, _check in well_formed:
        if check_id in seen:
            duplicates.append(check_id)
        seen.add(check_id)
    if duplicates:
        errors.append(
            f"run-plan contains duplicate check_id value(s): {sorted(set(duplicates))}"
        )

    actual = seen
    if actual != set(expected):
        errors.append(
            f"run-plan cells differ: actual={sorted(actual)} expected={sorted(expected)}"
        )
    for check_id, check in well_formed:
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
