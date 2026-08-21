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

    lines = [
        "### Cold/warm performance benchmark (P1-8, informational)",
        "",
        f"SHA: `{data.get('sha', '?')}`",
    ]
    scanner_sha = data.get("scanner_sha")
    if scanner_sha:
        lines.append(f"Scanner SHA: `{scanner_sha}`")
    lines.append("")

    # Bazel's own cold/warm pair -- present whenever this JSON came from
    # performance.yml's `benchmark` job. Absent from `profile_measurements`'s
    # own JSON (that job never builds the Bazel profile -- `benchmark`
    # already covers it), in which case every cell renders `—` rather
    # than this section being silently dropped: a reader of a
    # profile_measurements-only report should still see the shape of
    # what this table normally covers, not a missing section they'd have
    # to guess the reason for.
    bazel_cold = data.get("bazel_build_cold_ms")
    bazel_warm = data.get("bazel_build_warm_ms")
    dump_cold = data.get("abicheck_dump_cold_ms")
    dump_warm = data.get("abicheck_dump_warm_ms")
    lines += [
        "| Stage | Cold | Warm | Speedup |",
        "|---|---:|---:|---:|",
        f"| `bazel build //:math` | {_fmt_ms(bazel_cold)} | {_fmt_ms(bazel_warm)} | {_speedup(bazel_cold, bazel_warm)} |",
        f"| `abicheck dump` | {_fmt_ms(dump_cold)} | {_fmt_ms(dump_warm)} | {_speedup(dump_cold, dump_warm)} |",
        "",
        "Cold = disk/snapshot cache emptied first; warm = same cache, "
        "local build/dump state cleaned but the cache directory kept. "
        "Never gates -- see this workflow's own module comment.",
    ]

    # PR5 (design doc section 9.5): per-profile measurements, present only
    # in the `profile_measurements` job's own JSON -- absent (not an empty
    # list) from `benchmark`'s JSON, so this section is skipped there
    # rather than rendering an empty table with nothing to show.
    profiles = data.get("profiles")
    # CodeRabbit review, PR #24: a non-empty `profiles` list containing
    # only non-dict entries used to still pass the `profiles` truthiness
    # check above and render an empty table (a heading with no rows) --
    # filter to actual dict entries FIRST, so the section is only ever
    # rendered (and only ever looped over) when there is something real
    # to show.
    valid_profiles = [entry for entry in profiles if isinstance(entry, dict)] if isinstance(profiles, list) else []
    if valid_profiles:
        lines += [
            "",
            "### Per-profile build + dump (single cold run, PR5)",
            "",
            "| Profile | Build | `abicheck dump` |",
            "|---|---:|---:|",
        ]
        for entry in valid_profiles:
            profile_id = entry.get("profile_id", "?")
            lines.append(f"| `{profile_id}` | {_fmt_ms(entry.get('build_ms'))} | {_fmt_ms(entry.get('dump_ms'))} |")
        lines += [
            "",
            "One cold build + one cold dump per profile -- no separate warm "
            "phase (unlike Bazel's own disk-cache pair above); see this "
            "job's own workflow comment for why. Never gates.",
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
