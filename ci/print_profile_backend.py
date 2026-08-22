#!/usr/bin/env python3
"""Print one profile's `backend` field from ci/profiles.yaml.

A small, standalone script rather than an inline `python3 -c` one-liner
embedded in workflow YAML: three workflows (integration-shadow.yml,
profile-baseline.yml, release.yml) each need "does this matrix leg's
profile use the cmake/make backend" to decide whether to install the
real abicheck scanner + CastXML (ci/real_scan.py needs both; the bazel
backend needs neither). Looking this up from ci/profiles.yaml itself,
by id, is what actually scales to a future profile addition -- a
workflow `if:` condition hardcoding today's two cmake/make profile ids
would silently stop installing the scanner for a third one (Codex
review, PR #25).

Usage:

    python3 ci/print_profile_backend.py --profile-id <id> [--profiles-file PATH]

Prints the backend string (e.g. "cmake") to stdout and exits 0, or exits
1 with an error on stderr if the profile id isn't found.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES = REPO_ROOT / "ci" / "profiles.yaml"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profiles-file", type=Path, default=DEFAULT_PROFILES)
    args = parser.parse_args(argv)

    with open(args.profiles_file, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    for profile in data.get("profiles", []):
        if profile.get("id") == args.profile_id:
            print(profile["backend"])
            return 0

    print(f"print_profile_backend: no profile {args.profile_id!r} in {args.profiles_file}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
