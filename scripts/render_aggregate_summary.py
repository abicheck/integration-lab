#!/usr/bin/env python3
"""Render a short Markdown summary of an `abicheck aggregate` JSON report.

Architecture review P1-5 (multi-library aggregate validation), hardened by
the P0.6 fail-closed follow-up: the `aggregate` job in
`.github/workflows/abi-scan.yml` runs `abicheck aggregate` over whatever
per-target reports (`abi-report-<target>.json`) were uploaded this run,
gated against a declared expected-target manifest (`math` required
whenever `scan` judged the PR ABI-relevant; see that job's own comment),
and wants a small, human-readable table in the job summary -- this is
that renderer, kept as its own script (rather than an inline `python3 -c`
block in the workflow YAML) for the same reason every other `render_*.py`
script here is: the shape of `aggregate`'s JSON output
(`targets[].target_id`/`compatibility_verdict`, verified against a real
`abicheck aggregate --format json` run) is worth pinning in one place a
future abicheck version bump can update, not duplicating inline where a
YAML indentation mistake is easy to make invisible.

Degrades to a plain "nothing to aggregate" message when the report is
missing or empty -- reached only on the `--discovered-only` fallback path
(neither `math` nor `strings` was judged ABI-relevant this run, so
nothing was declared and there is nothing to gate), not on an
ABI-relevant PR that dropped a required report -- that case is now a real
`abicheck aggregate` coverage-axis failure (exit 1), which fails the step
before this renderer ever runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def render(report_path: Path) -> str:
    if not report_path.exists() or report_path.stat().st_size == 0:
        return (
            "### Aggregate report (P1-5)\n\n"
            "No per-target reports were available to aggregate this run.\n"
        )

    with report_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    lines = [
        "### Aggregate report (P1-5)",
        "",
        f"Status: `{data.get('status', '?')}`",
        "",
        "| Target | Verdict |",
        "|---|---|",
    ]
    for target in data.get("targets", []):
        lines.append(
            f"| {target.get('target_id', '?')} | {target.get('compatibility_verdict', '?')} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="abicheck aggregate --format json output")
    args = parser.parse_args()

    print(render(args.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
