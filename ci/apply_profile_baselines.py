#!/usr/bin/env python3
"""PR4 item 1 (design doc section 22 Phase C: accepted-main baseline
channel): validate a freshly `ci/check_profile.py dump`-ed baseline and,
if it's valid and actually different, write it over the committed
`abi/profiles/<profile-id>/<target>.abicheck.json`.

Why a separate script instead of writing straight from the per-profile
build job: the design doc is explicit that Phase B/C baseline refreshes
must "generate raw snapshots in parallel profile jobs, but normalize and
commit them only from one fan-in baseline job. Do not let matrix jobs
independently push." -- `.github/workflows/profile-baseline.yml`'s own
`refresh` job (one matrix leg per profile, `continue-on-error: true`,
read-only `contents: read`) only ever dumps to a per-profile artifact; this
script is what the fan-in `commit` job (the only job in that workflow with
`contents: write`) runs once, over every downloaded artifact, to decide
what actually gets written and committed.

Fails closed the same way `ci/check_profile.py`'s own baseline-consuming
code does (PR2's own `c04285a` commit: "fail closed on a baseline missing
dynamic_symbols/public_headers, don't silently default to empty"): a raw
dump that is missing (the profile's own build/dump step failed upstream --
this workflow's `refresh` job is `continue-on-error: true`, so a missing
artifact is an expected, not-crashing outcome here), unparseable, of the
wrong `kind`, stamped for a different profile_id/target, or has an empty
`dynamic_symbols`/`public_headers` is never written over a good committed
baseline -- each such target is reported `invalid`/`missing` and the
existing committed file is left exactly as it was, so one profile's flaky
build never regresses (or blanks) any profile's trusted baseline, and
every other, valid target still gets applied in the same run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CI_DIR = Path(__file__).resolve().parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from check_profile import BASELINE_KIND  # noqa: E402

VALID_STATUSES = frozenset({"updated", "unchanged", "invalid", "missing", "verified", "drift"})


def _validate_raw_baseline(doc: Any, profile_id: str, target: str) -> Optional[str]:
    """Returns None if doc is a well-formed baseline for profile_id/target,
    else a human-readable reason it is not."""
    if not isinstance(doc, dict):
        return f"not a JSON object (got {type(doc).__name__})"
    if doc.get("kind") != BASELINE_KIND:
        return f"kind={doc.get('kind')!r}, expected {BASELINE_KIND!r}"
    if doc.get("profile_id") != profile_id:
        return f"profile_id={doc.get('profile_id')!r}, expected {profile_id!r}"
    if doc.get("target") != target:
        return f"target={doc.get('target')!r}, expected {target!r}"
    symbols = doc.get("dynamic_symbols")
    if not isinstance(symbols, list) or not symbols:
        return f"dynamic_symbols is empty or not a list ({symbols!r})"
    headers = doc.get("public_headers")
    if not isinstance(headers, dict) or not headers:
        return f"public_headers is empty or not an object ({headers!r})"
    return None


def apply_one(
    profile_id: str, target: str, raw_path: Optional[Path], committed_path: Path, write: bool = True
) -> Dict[str, Any]:
    """write=True (the default; `.github/workflows/profile-baseline.yml`'s
    own use): apply a fresh raw dump over committed_path, returning
    `updated`/`unchanged` when it's valid.

    write=False (`.github/workflows/release.yml`'s own use, PR4 item 2 --
    "release-contract baseline publication"): never writes anything, only
    reports whether the raw dump matches what's ALREADY committed --
    `verified` (matches exactly) or `drift` (valid, but the release
    commit's own fresh build doesn't reproduce its own committed
    baseline -- a real integrity problem release.yml treats as fatal,
    never silently republished). `invalid`/`missing` mean the same thing
    either way: this target was not certifiable this run.
    """
    if raw_path is None or not raw_path.is_file():
        return {
            "target": target,
            "status": "missing",
            "detail": f"no raw dump at {raw_path} -- upstream build/dump step likely failed",
        }

    try:
        with open(raw_path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
        raw_doc = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return {"target": target, "status": "invalid", "detail": f"unreadable/invalid JSON: {exc}"}

    invalid_reason = _validate_raw_baseline(raw_doc, profile_id, target)
    if invalid_reason is not None:
        return {"target": target, "status": "invalid", "detail": invalid_reason}

    # ci/check_profile.py's own dump writer (_write_json) always uses
    # indent=2, sort_keys=True and this baseline schema has no timestamps/
    # absolute paths/per-run metadata (build_baseline()'s own docstring) --
    # so a byte-identical raw dump means a byte-identical committed file
    # is genuinely unchanged, not just "close enough". Re-serializing
    # here (rather than a raw string compare) makes that guarantee
    # independent of any whitespace this particular raw_path happened to
    # carry.
    normalized_text = json.dumps(raw_doc, indent=2, sort_keys=True) + "\n"
    existing_text = committed_path.read_text(encoding="utf-8") if committed_path.is_file() else None
    if existing_text == normalized_text:
        return {"target": target, "status": "unchanged" if write else "verified", "detail": None}

    if not write:
        return {
            "target": target,
            "status": "drift",
            "detail": (
                f"a fresh dump does not match the committed baseline at {committed_path} -- "
                "this exact commit's own build no longer reproduces its own accepted baseline"
            ),
        }

    committed_path.parent.mkdir(parents=True, exist_ok=True)
    committed_path.write_text(normalized_text, encoding="utf-8")
    return {"target": target, "status": "updated", "detail": None}


def apply_profile(
    profile_id: str, raw_paths: Dict[str, Path], committed_dir: Path, write: bool = True
) -> Dict[str, Any]:
    results = {
        target: apply_one(profile_id, target, raw_paths[target], committed_dir / f"{target}.abicheck.json", write=write)
        for target in sorted(raw_paths)
    }
    return {"profile_id": profile_id, "targets": results}


def _parse_kv(raw: str) -> tuple:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected TARGET=PATH, got {raw!r}")
    target, _, path = raw.partition("=")
    if not target or not path:
        raise argparse.ArgumentTypeError(f"expected TARGET=PATH, got {raw!r}")
    return target, Path(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument(
        "--raw",
        action="append",
        default=[],
        metavar="TARGET=PATH",
        help="a freshly dumped raw baseline for one target, repeatable",
    )
    parser.add_argument("--committed-dir", type=Path, required=True)
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="never write --committed-dir; report verified/drift instead of unchanged/updated (release.yml's use)",
    )
    args = parser.parse_args(argv)

    raw_paths = dict(_parse_kv(entry) for entry in args.raw)
    if not raw_paths:
        print("apply_profile_baselines: at least one --raw TARGET=PATH is required", file=sys.stderr)
        return 1

    result = apply_profile(args.profile_id, raw_paths, args.committed_dir, write=not args.verify_only)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for target, target_result in result["targets"].items():
        print(f"{args.profile_id}/{target}: {target_result['status']}" + (
            f" ({target_result['detail']})" if target_result.get("detail") else ""
        ))

    if args.verify_only:
        # release.yml's own use: invalid/missing/drift are all fatal here
        # (exit 1) -- a release must not be certified/published on a
        # target that doesn't provably reproduce its own committed
        # baseline. No exit-2 soft-warning tier in this mode.
        any_bad = any(t["status"] in ("invalid", "missing", "drift") for t in result["targets"].values())
        return 1 if any_bad else 0

    # write mode (profile-baseline.yml's own use): invalid/missing never a
    # HARD failure (exit 1, reserved for a bad CLI invocation) -- see the
    # module docstring's own "one profile's flaky build never regresses...
    # any profile's trusted baseline" contract: the commit job (this
    # script's only caller) still needs to commit whatever DID apply
    # cleanly even when another target failed. Exit 2 distinguishes "ran
    # fine, but at least one target was invalid/missing" (worth a
    # workflow-level ::warning::, per this module's own docstring) from
    # exit 0 (every requested target applied cleanly, nothing to warn
    # about) -- checkable with a plain `if`/`$?` in the calling workflow
    # step without any nested scripting.
    any_invalid = any(t["status"] in ("invalid", "missing") for t in result["targets"].values())
    return 2 if any_invalid else 0


if __name__ == "__main__":
    sys.exit(main())
