#!/usr/bin/env python3
"""Render capabilities.yaml's `gap`/`planned` entries into README.md's
"Known limitations / follow-ups" section, and check that README.md's
generated block still matches what capabilities.yaml actually says.

Why this exists: phase 1 (capabilities.yaml + check_capability_matrix.py,
see that script's own docstring) made the *coverage* claims machine-checked
against real workflow jobs, but README.md's "Known limitations" prose still
hand-typed its own copy of which axes aren't covered (`cc_shared_library`,
`MODULE.bazel.lock`) -- nothing stopped that copy drifting from
capabilities.yaml's own `status: gap` entries the way the pre-phase-1 prose
drifted from the real workflow jobs. This script closes that gap the same
way: capabilities.yaml is the one source of truth, this script renders it,
and `--check` fails CI if README.md's block disagrees with a fresh render.

Usage:
    gen_capability_gaps.py --check   # exit 1 + diff if README.md is stale
    gen_capability_gaps.py --write   # regenerate the block in place

Deliberately renders ONLY the gap/planned list, not the whole "Known
limitations" section -- the surrounding prose (why `//:math` is the one
`depth: source` target, the generated-header gotcha, the untested security
scenario) is hand-authored narrative with no capabilities.yaml counterpart
to generate it from, and folding it into the generated block would either
flatten it or force capabilities.yaml to grow fields that exist only to
reproduce prose. See MARKER_START/MARKER_END below for exactly what's
generated.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "gen_capability_gaps: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "capabilities.yaml"
README_PATH = REPO_ROOT / "README.md"

MARKER_START = "<!-- capability-matrix:gaps:start -->"
MARKER_END = "<!-- capability-matrix:gaps:end -->"

# Statuses this block enumerates -- `gap` (no follow-up scoped yet) and
# `planned` (a gap with a concrete follow-up already scoped, per
# capabilities.yaml's own status-vocabulary comment). `covered`/
# `non_gating_watch` entries are the *opposite* of what "Known limitations"
# is for and are deliberately excluded.
GAP_STATUSES = frozenset({"gap", "planned"})


def _load_matrix(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "capabilities" not in data:
        raise SystemExit(f"{path}: expected a top-level 'capabilities' key")
    return data


def render_block(matrix: dict[str, Any]) -> str:
    gaps = [e for e in matrix["capabilities"] if e.get("status") in GAP_STATUSES]
    lines = [MARKER_START]
    if not gaps:
        # Not just a hypothetical: this line is what stops the block
        # silently rendering empty (and therefore looking generated-but-
        # trivial) if every known gap is ever closed without anyone
        # updating this script.
        lines.append("_No known gaps -- every declared capability axis is covered._")
    else:
        for entry in gaps:
            note = (entry.get("note") or "").strip()
            # capabilities.yaml notes are YAML folded scalars (`>`), which
            # collapse internal newlines to spaces already -- re-collapse
            # any that remain (e.g. from a literal `|` block) so each gap
            # renders as one bullet, not a multi-line fragment that breaks
            # the surrounding Markdown list.
            note = " ".join(note.split())
            lines.append(f"- **{entry['id']}** (`{entry.get('status')}`): {note}")
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def _splice(readme_text: str, new_block: str) -> str:
    start = readme_text.find(MARKER_START)
    end = readme_text.find(MARKER_END)
    if start == -1 or end == -1:
        raise SystemExit(
            f"{README_PATH}: missing {MARKER_START!r}/{MARKER_END!r} markers -- "
            "add an empty marker pair where the generated gap list belongs"
        )
    if end < start:
        raise SystemExit(f"{README_PATH}: {MARKER_END!r} appears before {MARKER_START!r}")
    end += len(MARKER_END)
    return readme_text[:start] + new_block.rstrip("\n") + readme_text[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check", action="store_true", help="fail if README.md's block is stale"
    )
    group.add_argument(
        "--write", action="store_true", help="regenerate the block in README.md"
    )
    args = parser.parse_args()

    matrix = _load_matrix(MATRIX_PATH)
    new_block = render_block(matrix)
    readme_text = README_PATH.read_text()
    updated_text = _splice(readme_text, new_block)

    if args.write:
        README_PATH.write_text(updated_text)
        print(f"gen_capability_gaps: wrote {README_PATH}")
        return 0

    if updated_text != readme_text:
        print(
            "gen_capability_gaps: README.md's generated gap list is stale "
            "relative to capabilities.yaml -- run "
            "`python3 scripts/gen_capability_gaps.py --write` and commit the result",
            file=sys.stderr,
        )
        return 1

    n = sum(1 for e in matrix["capabilities"] if e.get("status") in GAP_STATUSES)
    print(f"gen_capability_gaps: OK -- README.md matches capabilities.yaml ({n} gap(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
