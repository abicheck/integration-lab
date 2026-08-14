#!/usr/bin/env python3
"""Render a Markdown cold/warm summary table from performance.yml's timings JSON.

Architecture review P1-8 (cold/warm performance benchmarks). Kept as its
own script rather than an inline `python3 -c` block in the workflow YAML
for the same reason `render_aggregate_summary.py` is: a multi-line Python
block embedded in a YAML `run: |` scalar is easy to break with an
indentation mistake that YAML accepts silently (that exact mistake was
made and caught while building `render_aggregate_summary.py` -- see this
repo's own history), and a dedicated script is testable in isolation.

A `null` timing (a stage that never ran -- e.g. the cold Bazel build
failed, so the warm one and both abicheck dump steps never got a chance
to run either) renders as `—` rather than crashing or silently omitting
the row, so a partial/failed benchmark run is still legible from its own
job summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt_ms(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:,} ms"


def _speedup(cold: object, warm: object) -> str:
    if not isinstance(cold, (int, float)) or not isinstance(warm, (int, float)) or warm <= 0:
        return "—"
    return f"{cold / warm:.2f}×"


def render(timings_path: Path) -> str:
    with timings_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    bazel_cold = data.get("bazel_build_cold_ms")
    bazel_warm = data.get("bazel_build_warm_ms")
    dump_cold = data.get("abicheck_dump_cold_ms")
    dump_warm = data.get("abicheck_dump_warm_ms")

    lines = [
        "### Cold/warm performance benchmark (P1-8, informational)",
        "",
        f"SHA: `{data.get('sha', '?')}`",
        "",
        "| Stage | Cold | Warm | Speedup |",
        "|---|---:|---:|---:|",
        f"| `bazel build //:math` | {_fmt_ms(bazel_cold)} | {_fmt_ms(bazel_warm)} | {_speedup(bazel_cold, bazel_warm)} |",
        f"| `abicheck dump` | {_fmt_ms(dump_cold)} | {_fmt_ms(dump_warm)} | {_speedup(dump_cold, dump_warm)} |",
        "",
        "Cold = disk/snapshot cache emptied first; warm = same cache, "
        "local build/dump state cleaned but the cache directory kept. "
        "Never gates -- see this workflow's own module comment.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timings", type=Path, help="performance.yml's timings JSON")
    args = parser.parse_args()

    print(render(args.timings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
