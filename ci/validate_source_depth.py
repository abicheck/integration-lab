#!/usr/bin/env python3
"""Fail closed unless an ABICheck report proves the requested source depth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _depths(report: dict[str, Any]) -> tuple[Any, Any]:
    assurance = report.get("analysis_assurance")
    if isinstance(assurance, dict):
        return assurance.get("requested_depth"), assurance.get("effective_depth")
    provenance = report.get("dump_provenance")
    if isinstance(provenance, dict):
        return provenance.get("requested_depth"), provenance.get("effective_depth")
    level = report.get("level")
    if isinstance(level, dict):
        # Older scan reports carry only effective depth here. The workflow's
        # explicit --depth argument supplies the requested side.
        return level.get("requested_depth"), level.get("depth")
    return None, None


def validate(report: dict[str, Any], expected: str = "source") -> list[str]:
    errors = []
    requested, effective = _depths(report)
    if requested not in (None, expected):
        errors.append(f"requested_depth={requested!r}, expected {expected!r}")
    if effective != expected:
        errors.append(f"effective_depth={effective!r}, expected {expected!r}")
    assurance = report.get("analysis_assurance")
    if isinstance(assurance, dict) and assurance.get("status") != "complete":
        errors.append(f"analysis_assurance.status={assurance.get('status')!r}, expected 'complete'")
    operational = report.get("operational_errors")
    if operational:
        errors.append(f"report has {len(operational)} operational error(s)")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--expected", default="source")
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read report: {exc}")
        return 1
    if not isinstance(report, dict):
        print("ERROR: report must be a JSON object")
        return 1
    errors = validate(report, args.expected)
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
