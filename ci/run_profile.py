#!/usr/bin/env python3
"""Driver: run one profile end-to-end (verify_environment -> clean -> build
-> collect_evidence -> stage -> emit build-output.json) and print a
one-line JSON summary. This is the script
.github/workflows/integration-shadow.yml invokes per matrix entry -- it is
a thin, GitHub-Actions-agnostic wrapper over ci/backends/ +
ci/emit_build_output.py (no `GITHUB_*` env var read, no workflow command
written), so it's equally usable from a local shell.

Exit code reflects whether the profile's own build succeeded (0) or not
(1) -- the shadow workflow's own job step wraps this in
`continue-on-error: true` so a profile-specific failure never fails the
overall workflow (PR1 is a shadow/advisory build, not a new gate).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CI_DIR = Path(__file__).resolve().parent
REPO_ROOT = CI_DIR.parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from emit_build_output import CI_DIR as _CI_DIR, build_and_stage  # noqa: E402
from select_profiles import load_profiles  # noqa: E402
from validate_build_output import validate_file  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profiles-file", type=Path, default=CI_DIR / "profiles.yaml")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    profiles = load_profiles(args.profiles_file)
    if args.profile_id not in profiles:
        print(json.dumps({"profile_id": args.profile_id, "success": False, "error": "unknown profile id"}))
        return 1

    try:
        doc = build_and_stage(
            args.profile_id,
            profiles_path=args.profiles_file,
            repo_root=args.repo_root,
            out_root=args.out_dir,
        )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(json.dumps({"profile_id": args.profile_id, "success": False, "error": str(exc)}))
        return 1

    out_dir = args.out_dir / f"abicheck-build-{args.profile_id}"
    schema_errors = validate_file(out_dir / "build-output.json")

    summary = {
        "profile_id": args.profile_id,
        "backend": doc["profile"]["backend"],
        "contract": doc["profile"]["contract"],
        "success": doc["success"],
        "schema_valid": not schema_errors,
        "schema_errors": schema_errors,
        "staged_dir": str(out_dir),
        "diagnostics": doc["diagnostics"],
    }
    print(json.dumps(summary))
    return 0 if (doc["success"] and not schema_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
