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
import difflib
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

# check_capability_matrix.py is this script's own sibling in scripts/ --
# Python puts a script's own directory on sys.path, so this is a plain
# same-directory import, not a packaging dependency. Reused rather than
# reimplemented (CodeRabbit review, PR #16): capabilities.yaml's full
# schema shape (every entry a mapping, `id`/`status` present and valid) is
# already validated there, and duplicating that logic here would just be a
# second copy to drift out of sync with the first.
import check_capability_matrix

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
    if not isinstance(data, dict) or "capabilities" not in data or "dimensions" not in data:
        raise SystemExit(f"{path}: expected top-level 'dimensions' and 'capabilities' keys")
    # Full schema validation (every entry a mapping, id/status well-formed,
    # job/workflow references real) lives in check_capability_matrix.py --
    # reused here rather than assumed, so a malformed capabilities.yaml
    # fails with the same clear diagnostics in this script as it would in
    # that one, instead of a raw TypeError/KeyError from render_block()
    # further down (CodeRabbit review, PR #16).
    errors = check_capability_matrix.check(data)
    if errors:
        raise SystemExit(
            f"{path}: {len(errors)} schema problem(s) -- run "
            "`python3 scripts/check_capability_matrix.py` for details:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return data


def render_block(matrix: dict[str, Any]) -> str:
    gaps = [e for e in matrix["capabilities"] if e.get("status") in GAP_STATUSES]
    lines = [MARKER_START]
    if not gaps:
        # Not just a hypothetical: this line is what stops the block
        # silently rendering empty (and therefore looking generated-but-
        # trivial) if every known gap is ever closed without anyone
        # updating this script.
        #
        # Deliberately does NOT claim "every axis is covered" (Codex
        # review, PR #16): this script only knows that no entry currently
        # has status gap/planned -- it never checks that every possible
        # dimension combination actually has a covered/non_gating_watch
        # entry naming it, so an accidental deletion of the two real gap
        # entries would make this line assert a completeness this script
        # never verified. States only what's actually true instead.
        lines.append(
            "_No `gap`/`planned` entries are currently declared in "
            "`capabilities.yaml`._"
        )
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
    # Exactly one marker pair, not just "at least one" -- a stray or
    # duplicated marker (e.g. from a bad merge) would otherwise let --write
    # silently splice into the wrong range or leave a second, unmanaged
    # generated block behind (CodeRabbit review, PR #16).
    if readme_text.count(MARKER_START) != 1 or readme_text.count(MARKER_END) != 1:
        raise SystemExit(
            f"{README_PATH}: expected exactly one {MARKER_START!r}/"
            f"{MARKER_END!r} marker pair, found "
            f"{readme_text.count(MARKER_START)} start(s) and "
            f"{readme_text.count(MARKER_END)} end(s)"
        )
    start = readme_text.find(MARKER_START)
    end = readme_text.find(MARKER_END, start + len(MARKER_START))
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
        diff = difflib.unified_diff(
            readme_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=str(README_PATH),
            tofile=f"{README_PATH} (generated)",
        )
        print(
            "gen_capability_gaps: README.md's generated gap list is stale "
            "relative to capabilities.yaml -- run "
            "`python3 scripts/gen_capability_gaps.py --write` and commit the result\n",
            file=sys.stderr,
        )
        sys.stderr.writelines(diff)
        return 1

    n = sum(1 for e in matrix["capabilities"] if e.get("status") in GAP_STATUSES)
    print(f"gen_capability_gaps: OK -- README.md matches capabilities.yaml ({n} gap(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
