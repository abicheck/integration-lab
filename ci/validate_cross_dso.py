#!/usr/bin/env python3
"""Verify staged math/strings really consume the staged core provider."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


NEEDED_RE = re.compile(r"Shared library: \[([^]]+)\]")


def needed(binary: Path) -> set[str]:
    result = subprocess.run(["readelf", "-d", str(binary)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return set(NEEDED_RE.findall(result.stdout))


def validate(root: Path) -> list[str]:
    errors = []
    libdir = root / "artifacts/lib"
    for name in ("libmath.so", "libstrings.so"):
        binary = libdir / name
        if not binary.is_file():
            errors.append(f"missing staged consumer DSO {binary}")
            continue
        if "libcore.so" not in needed(binary):
            errors.append(f"{name} does not declare DT_NEEDED libcore.so")
    core = libdir / "libcore.so"
    if not core.is_file():
        errors.append(f"missing staged provider DSO {core}")
    app = root / "artifacts/bin/consumer_app"
    if not errors and app.is_file():
        env = dict(os.environ, LD_LIBRARY_PATH=str(libdir))
        result = subprocess.run([str(app)], env=env, capture_output=True, text=True)
        if result.returncode:
            errors.append(f"staged consumer_app failed ({result.returncode}): {result.stderr}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_output", type=Path)
    args = parser.parse_args()
    try:
        errors = validate(args.build_output)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
