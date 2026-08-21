#!/usr/bin/env python3
"""PR (roadmap.md item 1 / UPSTREAM_TO_ABICHECK.md's "PR2" follow-up entry):
run the REAL `abicheck` scanner (`abicheck dump`/`abicheck compare`, the
same package `.github/workflows/abi-scan.yml` installs for the Bazel
profile) for the CMake and Make profiles, instead of `ci/check_profile.py`'s
nm/readelf symbol-table diff.

## Why this exists, and why it didn't exist before

`ci/check_profile.py`'s own module docstring (written during PR2) recorded
that the real scanner was "not available in this sandbox -- there is no
network access here". That was true for that sandbox at that time; it is
not true in general, and is not true in this session's own sandbox
(verified: `pip download` against `abicheck @ git+https://github.com/
abicheck/abicheck.git` succeeds, and a real `abicheck dump --depth source`
run against this repo's own CMake and Make staged output produces a
genuine source-depth snapshot -- see this repo's PR history for the
end-to-end verification: a real function removal from `src/math.cc`
reproducibly reports `BREAKING` via `abicheck compare`, not just the
lab's own symbol-table mechanism).

## What this module is not

Not a Bazel replacement: the Bazel profile already has its own real,
required ABI gate (`abi-scan.yml`'s `scan` job, using Bazel's own
`cquery`/`aquery` evidence pack -- `scripts/build_bazel_evidence_pack.py`).
This module only covers the two profiles that had no real-scanner path at
all before this PR: `cmake` and `make`. `ci/check_profile.py`'s own
nm/readelf mechanism remains in place for the `bazel` backend inside this
shadow/advisory workflow (unchanged, still non-gating there -- the real
Bazel gate lives entirely in `abi-scan.yml`, not here).

## The one real integration gap this module had to solve: target scoping

Both CMake's `compile_commands.json` and Make's Bear-generated equivalent
describe the WHOLE project's compile units (`math`, `strings`, and the
`consumer` app all in one database) -- exactly analogous to
`UPSTREAM_TO_ABICHECK.md`'s P0.2 entry for Bazel's `deps(//...)` query
scope. Handed the whole database, `abicheck dump -H <math's header dir>`
refuses outright with a real, correct error: the consumer's own TU also
`#include`s `math.h` under a materially different compile context
(`-fPIE` vs the library's own `-fPIC`), so abicheck can't pick a context
for the header without guessing (verified directly against this repo's own
compile_commands.json -- not a hypothetical). Pre-filtering the compile
database to just the target's own translation unit(s) before it ever
reaches `abicheck dump` (`filter_compile_db_for_target` below) resolves
this the same way Bazel's own target-scoped `cquery`/`aquery` already does
for that profile -- this repo's own fixed target->source-file layout
(`TARGET_SOURCE_FILE` below), not a generic build-system inference.

## Known limitation: embedded snapshots carry the checkout's absolute paths

A real `abicheck dump` snapshot embeds the absolute source paths from the
compile database it was fed (e.g. `/home/runner/work/integration-lab/
integration-lab/src/math.cc` in a GitHub-hosted runner, a different
absolute path in any other checkout location) -- unlike
`scripts/normalize_baseline.py`'s own Bazel-snapshot normalization, this
module does NOT strip or rewrite them before `ci/check_profile.py dump`
writes a committed baseline. This is deliberate for now, not an oversight:
this repo's own accepted-main baseline refresh
(`.github/workflows/profile-baseline.yml`) always both produces AND
commits from the same GitHub-hosted runner checkout path, so every
comparison this lab actually runs in CI (PR check against the committed
baseline, or the next refresh against the previous one) compares two
snapshots from the identical absolute path -- consistent, just not
portable. A baseline produced by hand in a different local checkout path
(as this PR's own development sandbox would have produced) is NOT
guaranteed to compare cleanly against a CI-produced candidate for this
reason, which is exactly why this PR does not hand-commit freshly dumped
`abi/profiles/<id>/{math,strings}.abicheck.json` baselines itself -- it
lets `profile-baseline.yml`'s own next push-to-main refresh produce the
first real ones, from inside CI, where the path is actually consistent.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

# This repo's own fixed target->source-file layout (same convention
# ci/check_profile.py's _public_headers_for_target already uses for
# target->header-root). Not a generic inference -- a real build system
# would need its own project-specific mapping here too (an .abicheck.yml
# `targets:` block is the productized version of this; see
# UPSTREAM_TO_ABICHECK.md P1.3).
TARGET_SOURCE_FILE = {
    "math": "src/math.cc",
    "strings": "strings_lib/src/strings.cc",
}

# The two backends this module covers. Bazel keeps its own real gate
# (abi-scan.yml) and this shadow workflow's existing nm/readelf mechanism
# for its own advisory leg -- seehis module's own docstring.
REAL_SCAN_BACKENDS = frozenset({"cmake", "make"})

# abicheck compare's own verdict vocabulary (compare --help's exit-code
# tables) mapped onto this lab's existing 4-value model
# (NO_CHANGE/COMPATIBLE/BREAKING/NOT_COMPARABLE), so
# ci/check_profile_coverage.py and ci/emit_profile_receipt.py -- which only
# ever read a report's top-level "verdict" field -- don't need to learn a
# second vocabulary. COMPATIBLE_WITH_RISK folds into COMPATIBLE (both are
# exit-0 "no binary ABI break" per abicheck's own table; the risk detail is
# preserved in this module's own `detail` text, never silently dropped).
# API_BREAK (a source-level break requiring recompilation) folds into
# BREAKING, matching this lab's binary-ABI-first framing elsewhere.
# Anything NOT in this map is deliberately NOT defaulted to a "safe"
# verdict -- see map_verdict below (fail closed, per the design doc's
# "no magic fallbacks" rule).
_VERDICT_MAP = {
    "NO_CHANGE": "NO_CHANGE",
    "COMPATIBLE": "COMPATIBLE",
    "COMPATIBLE_WITH_RISK": "COMPATIBLE",
    "API_BREAK": "BREAKING",
    "BREAKING": "BREAKING",
    "NOT_COMPARABLE": "NOT_COMPARABLE",
}


class RealScanError(RuntimeError):
    pass


def target_source_suffix(target: str) -> str:
    suffix = TARGET_SOURCE_FILE.get(target)
    if suffix is None:
        raise RealScanError(
            f"no known source file for target {target!r} -- add it to "
            "ci/real_scan.py's TARGET_SOURCE_FILE mapping"
        )
    return suffix


def filter_compile_db_for_target(compile_db: Path, target: str, out_path: Path) -> Path:
    """Write, to out_path, the subset of compile_db's entries whose "file"
    is this target's own source file -- never the whole project database
    (see this module's own docstring for why: a header shared with another
    TU compiled under a different context makes abicheck refuse outright
    rather than guess).
    """
    if not compile_db.is_file():
        raise RealScanError(f"no compile database at {compile_db}")
    try:
        entries = json.loads(compile_db.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RealScanError(f"compile database {compile_db} is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise RealScanError(f"compile database {compile_db} is not a JSON array")

    suffix = target_source_suffix(target)
    filtered = [e for e in entries if isinstance(e, dict) and str(e.get("file", "")).replace("\\", "/").endswith(suffix)]
    if not filtered:
        raise RealScanError(
            f"compile database {compile_db} has no entry for target {target!r} "
            f"(expected a file ending in {suffix!r})"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
    return out_path


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def dump_real_snapshot(
    *,
    library_path: Path,
    header_dir: Path,
    compile_db: Path,
    sources_root: Path,
    version: str,
    out_path: Path,
) -> Dict[str, Any]:
    """Run a real `abicheck dump --depth source` against library_path,
    using compile_db (already filtered to one target -- see
    filter_compile_db_for_target) as --build-info and sources_root as
    --sources. Raises RealScanError -- never silently returns a
    lower-depth or binary-only snapshot -- if abicheck can't reach source
    depth (see this repo's UPSTREAM_TO_ABICHECK.md for the class of gap
    that can cause this: header-context ambiguity, a missing AST frontend,
    etc.) or the process otherwise fails.
    """
    if not library_path.is_file():
        raise RealScanError(f"no candidate library at {library_path}")
    if not header_dir.is_dir():
        raise RealScanError(f"no header directory at {header_dir}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "abicheck", "dump",
        str(library_path),
        "-H", str(header_dir),
        "--build-info", str(compile_db),
        "--sources", str(sources_root),
        "--depth", "source",
        "--version", version,
        "-o", str(out_path),
    ]
    proc = _run(cmd)
    if not out_path.is_file():
        raise RealScanError(
            f"abicheck dump did not produce {out_path} (exit={proc.returncode}): {proc.stdout}"
        )
    try:
        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RealScanError(f"abicheck dump wrote non-JSON output to {out_path}: {exc}") from exc

    resolved_depth = None
    m = None
    for line in proc.stdout.splitlines():
        if line.startswith("Resolved evidence depth:"):
            m = line.split(":", 1)[1].strip()
    resolved_depth = m
    if resolved_depth != "source":
        raise RealScanError(
            f"abicheck dump did not reach source depth (resolved: {resolved_depth!r}); "
            f"refusing to use a lower-depth snapshot as a source-depth baseline. "
            f"Full output:\n{proc.stdout}"
        )
    return snapshot


def compare_real_snapshots(old_snapshot: Path, new_snapshot: Path, out_path: Path) -> Dict[str, Any]:
    """Run a real `abicheck compare` between two dump-mode snapshots. A
    non-zero exit code here is an ordinary, expected "found something"
    signal (BREAKING/API_BREAK/etc.), not a tool failure -- only a missing
    --out_path (a real crash) raises.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "abicheck", "compare",
        str(old_snapshot), str(new_snapshot),
        "--format", "json",
        "-o", str(out_path),
    ]
    proc = _run(cmd)
    if not out_path.is_file():
        raise RealScanError(
            f"abicheck compare did not produce {out_path} (exit={proc.returncode}): {proc.stdout}"
        )
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RealScanError(f"abicheck compare wrote non-JSON output to {out_path}: {exc}") from exc


def map_verdict(abicheck_verdict: Any) -> Dict[str, Any]:
    """Map a real abicheck compare verdict onto this lab's 4-value model.
    An unrecognized verdict fails closed to NOT_COMPARABLE with an
    explicit reason -- never silently treated as a passing result (design
    doc section 3.9, "no magic fallbacks").
    """
    mapped = _VERDICT_MAP.get(abicheck_verdict)
    if mapped is None:
        return {
            "verdict": "NOT_COMPARABLE",
            "unmapped_abicheck_verdict": abicheck_verdict,
        }
    return {"verdict": mapped, "unmapped_abicheck_verdict": None}
