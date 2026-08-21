#!/usr/bin/env python3
"""PR4 item 2 (design doc section 22 Phase D: "release-contract baseline
publication"). Writes the provenance manifest that accompanies a
release's published per-profile baseline assets in
`.github/workflows/release.yml`: release tag, source commit, generation,
profile id, and a sha256 digest of each attached baseline file, so a
consumer downloading these assets from a GitHub Release doesn't have to
trust the filenames alone.

Doesn't verify anything itself -- `.github/workflows/release.yml`'s own
"Verify this release commit reproduces its own committed baseline" step
(`ci/apply_profile_baselines.py --verify-only`) is what decides whether a
release is even certifiable; this script only runs after that step has
already passed, to describe what's about to be published.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

CI_DIR = Path(__file__).resolve().parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from check_profile import MECHANISM_NOTE  # noqa: E402

SCHEMA_VERSION = 1
KIND = "abicheck.release-baseline-manifest/v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    release_tag: str,
    source_sha: str,
    generation: int,
    profile_id: str,
    baseline_paths: Dict[str, Path],
    generated_at: str,
) -> dict:
    targets = {
        target: {"path": str(path), "sha256": _sha256_file(path)}
        for target, path in sorted(baseline_paths.items())
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "release_tag": release_tag,
        "source_sha": source_sha,
        "generation": generation,
        "profile_id": profile_id,
        "mechanism": MECHANISM_NOTE,
        "targets": targets,
        "generated_at": generated_at,
    }


def _parse_kv(raw: str) -> tuple:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected TARGET=PATH, got {raw!r}")
    target, _, path = raw.partition("=")
    if not target or not path:
        raise argparse.ArgumentTypeError(f"expected TARGET=PATH, got {raw!r}")
    return target, Path(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--baseline", action="append", default=[], metavar="TARGET=PATH", required=True)
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args(argv)

    baseline_paths = dict(_parse_kv(entry) for entry in args.baseline)
    missing = [str(p) for p in baseline_paths.values() if not p.is_file()]
    if missing:
        print(f"emit_release_manifest: missing baseline file(s): {missing}", file=sys.stderr)
        return 1

    manifest = build_manifest(
        release_tag=args.release_tag,
        source_sha=args.source_sha,
        generation=args.generation,
        profile_id=args.profile_id,
        baseline_paths=baseline_paths,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"profile_id": args.profile_id, "release_tag": args.release_tag, "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
