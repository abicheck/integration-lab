#!/usr/bin/env python3
"""Merge N per-source-file abicheck-clang-plugin fact directories (each
shaped like `<dir>/manifest.json` + `<dir>/source_facts/*.jsonl`, as
`tools/abicheck/facts.bzl`'s `abicheck_facts_aspect` produces one of per
compiled translation unit) into ONE `abicheck_inputs/`-shaped pack: the
directory `abicheck compare --build-info <dir>` / `abicheck dump --build-info
<dir>` expects.

Usage: merge_abicheck_facts.py <out_dir> <facts_dir>...

Per-TU filenames already embed a source-content hash (see facts.bzl's own
docstring), so this is a plain flatten-and-copy: no filename collision is
possible across genuinely different TUs, and a re-run of the identical
sources is byte-identical, so copying "the last one wins" for any
accidental exact-duplicate is harmless.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {argv[0]} <out_dir> <facts_dir>...", file=sys.stderr)
        return 2

    out_dir = Path(argv[1])
    facts_dirs = [Path(p) for p in argv[2:]]

    out_source_facts = out_dir / "source_facts"
    out_source_facts.mkdir(parents=True, exist_ok=True)

    manifest_template: dict | None = None
    merged_files = 0

    for facts_dir in facts_dirs:
        manifest_path = facts_dir / "manifest.json"
        if manifest_template is None and manifest_path.is_file():
            manifest_template = json.loads(manifest_path.read_text())

        src_facts_dir = facts_dir / "source_facts"
        if not src_facts_dir.is_dir():
            continue
        for jsonl_path in src_facts_dir.glob("*.jsonl"):
            shutil.copy2(jsonl_path, out_source_facts / jsonl_path.name)
            merged_files += 1

    if manifest_template is None:
        # No constituent directory produced a manifest -- e.g. every
        # facts_dir was for a target with zero eligible sources. Emit the
        # same static shape the plugin itself writes (verified empirically
        # against a real build) so a downstream `abicheck ... --build-info`
        # still gets a well-formed (if empty) pack instead of a missing file.
        manifest_template = {
            "abicheck_inputs_version": 1,
            "binary": "",
            "compile_db": "",
            "created_by": "abicheck-bazel-lab merge_abicheck_facts.py",
            "exported_symbols": [],
            "headers": [],
            "kind": "abicheck_inputs",
            "library": "",
            "source_facts": ["source_facts"],
            "version": "",
        }

    (out_dir / "manifest.json").write_text(json.dumps(manifest_template, indent=2))

    print(
        f"Merged {merged_files} source_facts/*.jsonl file(s) from "
        f"{len(facts_dirs)} directory(ies) into {out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
