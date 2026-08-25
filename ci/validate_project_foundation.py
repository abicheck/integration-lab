#!/usr/bin/env python3
"""Fail closed when native project, producer profiles, and build output drift."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def validate(config_path: Path, profiles_path: Path, build_root: Path) -> list[str]:
    receipt = build_root / "build-output.json"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        producer = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
        output = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        return [f"cannot load project foundation input: {exc}"]
    if not isinstance(config, dict) or not isinstance(producer, dict) or not isinstance(output, dict):
        return ["project configuration, profile manifest, and build output must be mappings"]
    errors: list[str] = []

    native_profiles = config.get("profiles", {})
    producer_profiles = {entry["id"]: entry for entry in producer.get("profiles", [])}
    if set(native_profiles) != set(producer_profiles):
        errors.append(
            "profile ids differ: native="
            f"{sorted(native_profiles)} producer={sorted(producer_profiles)}"
        )

    profile_id = output.get("profile", {}).get("id")
    declared = producer_profiles.get(profile_id)
    if declared is None:
        errors.append(f"build output names undeclared profile {profile_id!r}")
        return errors

    actual_targets = {target.get("id") for target in output.get("targets", [])}
    expected_targets = set(declared.get("targets", {})) - set(
        declared.get("native_exclude_targets", [])
    )
    if actual_targets != expected_targets:
        errors.append(
            f"{profile_id}: target ids differ: output={sorted(actual_targets)} "
            f"producer={sorted(expected_targets)}"
        )

    native_libraries = {
        name for name, target in config.get("targets", {}).items()
        if target.get("kind", "library") == "library"
    }
    missing_libraries = native_libraries - actual_targets
    if missing_libraries:
        errors.append(f"{profile_id}: missing native libraries {sorted(missing_libraries)}")

    source_targets = {
        name for name, target in config.get("targets", {}).items()
        if any(check.get("depth") == "source" for check in target.get("checks", []))
    }
    output_targets = {target.get("id"): target for target in output.get("targets", [])}
    for name in sorted(source_targets):
        evidence = output_targets.get(name, {}).get("evidence") or {}
        if evidence.get("projection") != "declared" or not evidence.get("path"):
            errors.append(
                f"{profile_id}: source-required target {name!r} lacks a declared per-target evidence pack"
            )

    compiler = output.get("profile", {}).get("compiler", {})
    for field in ("family", "version", "path", "digest", "standard", "abi_macros"):
        if not compiler.get(field):
            errors.append(f"{profile_id}: compiler provenance missing {field!r}")
    if compiler.get("family") != declared.get("compiler", {}).get("family"):
        errors.append(f"{profile_id}: compiler family disagrees with producer declaration")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument(
        "--build-root", type=Path, required=True,
        help="staged abicheck-build-<profile-id>/ directory",
    )
    args = parser.parse_args()
    errors = validate(args.config, args.profiles, args.build_root)
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print("OK: native project, producer profile, and build output agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
