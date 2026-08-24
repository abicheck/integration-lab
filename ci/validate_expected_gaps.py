#!/usr/bin/env python3
"""Validate that every known product gap has a precise failure contract."""
from __future__ import annotations

import argparse
from pathlib import Path
import yaml


def validate(path: Path) -> list[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors, seen = [], set()
    for index, gap in enumerate(doc.get("expected_gaps", [])):
        prefix = f"expected_gaps[{index}]"
        gap_id = gap.get("id")
        if not gap_id or gap_id in seen:
            errors.append(f"{prefix}: id must be non-empty and unique")
        seen.add(gap_id)
        if gap.get("status") != "expected_gap":
            errors.append(f"{prefix}: status must be expected_gap")
        if not gap.get("upstream_issue"):
            errors.append(f"{prefix}: upstream_issue is required")
        failure = gap.get("expected_failure") or {}
        if not failure.get("phase") or not failure.get("reason"):
            errors.append(f"{prefix}: expected_failure.phase and reason are required")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    errors = validate(args.manifest)
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
