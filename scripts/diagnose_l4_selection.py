#!/usr/bin/env python3
"""Name the translation units behind a gate's L4 source-replay counts.

Diagnostic only -- never gates (the workflow step running it is
continue-on-error and its exit code is always 0). The compare report the
required gate produces carries only TU *counts*
(`analysis_assurance.translation_units`, the `L4_source_abi` detail string);
when the coverage contract fails on "only P/S selected TUs parsed", nobody
can tell from the CI log which TUs were selected or which failed.

This prints, from two independent sources:

1. the lab's own Bazel evidence pack (`--pack`): the compile units
   `aquery deps(//:math)` resolved -- i.e. what the gate *meant* to hand
   replay;
2. a candidate-side `abicheck dump` snapshot (`--snapshot`, produced by the
   workflow with the gate's own sources/build-info/depth inputs): the compile
   units abicheck actually merged, the replay extractor records, and every
   `source_abi:` diagnostic (one per failed TU).

Comparing (1) with (2) answers whether replay was handed more than the
target (zero-config `--sources` discovery widening the set) and which files
failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _pack_units(pack: Path) -> list[str]:
    try:
        from abicheck.buildsource import pack_io
        loaded = pack_io.load(pack)
    except Exception as exc:  # diagnostic: report, never raise
        return [f"<could not load pack {pack}: {exc}>"]
    ev = getattr(loaded, "build_evidence", None)
    if ev is None:
        return ["<pack has no build_evidence>"]
    return sorted(f"{getattr(cu, 'source', '?')} (target={getattr(cu, 'target_id', None)})" for cu in ev.compile_units)


def _snapshot_build_source(snapshot: Path) -> dict:
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    sections = data.get("sections")
    if isinstance(sections, dict):
        return ((sections.get("build") or {}).get("payload") or {}).get("build_source") or {}
    return data.get("build_source") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()

    if args.pack:
        units = _pack_units(args.pack)
        print(f"## Lab Bazel evidence pack ({args.pack}): {len(units)} compile unit(s)")
        for u in units:
            print(f"  pack CU: {u}")
    if args.snapshot:
        if not args.snapshot.is_file():
            print(f"## candidate snapshot {args.snapshot} missing -- dump failed")
            return 0
        bs = _snapshot_build_source(args.snapshot)
        be = bs.get("build_evidence") or {}
        cus = sorted(f"{cu.get('source')} (target={cu.get('target_id')})" for cu in be.get("compile_units") or [])
        print(f"## Candidate snapshot merged compile units: {len(cus)}")
        for u in cus:
            print(f"  merged CU: {u}")
        for ex in (bs.get("manifest") or {}).get("extractors") or []:
            print(f"  extractor {ex.get('name')}: status={ex.get('status')} detail={ex.get('detail')!r}")
        diags = [d for d in be.get("diagnostics") or [] if isinstance(d, str)]
        print(f"## build diagnostics ({len(diags)})")
        for d in diags:
            print(f"  {d}")
        cov = (bs.get("source_abi") or {}).get("coverage") or {}
        print(
            "## source_abi coverage: "
            + json.dumps({k: cov.get(k) for k in (
                "replay_scope", "compile_units_selected", "compile_units_parsed",
                "extractor_failures", "matched_symbols", "exported_symbols")})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
