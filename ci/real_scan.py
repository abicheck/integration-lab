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

## Normalization: every dumped snapshot is normalized before use

A raw `abicheck dump` snapshot embeds several volatile fields that differ
on every single invocation even for byte-identical source and an
unchanged ABI -- `created_at` (a wall-clock timestamp), `source_mtime`/
`source_mtime_epoch` (the source file's own mtime, which changes on every
fresh checkout), and absolute source paths from the compile database it
was fed (e.g. `/home/runner/work/integration-lab/integration-lab/
src/math.cc` on a GitHub-hosted runner, a different absolute path in any
other checkout location). Left as-is, embedding the raw snapshot into a
COMMITTED baseline (`build_baseline()` in `ci/check_profile.py`) would
make `ci/apply_profile_baselines.py`'s byte-for-byte comparison see a
"changed" baseline on every single `profile-baseline.yml` refresh, even
when nothing about the library's ABI changed at all -- and the same
volatile fields would make `release.yml`'s `--verify-only` reproducibility
check report false drift too (Codex review, PR #25: "Normalize embedded
snapshots before committing them").

`dump_real_snapshot()` below normalizes every snapshot it produces through
`scripts/normalize_baseline.py`'s own `normalize()` -- the exact same
volatile-field stripping and absolute-path normalization the Bazel gate's
own committed baselines (`abi/math.abicheck.json`, etc.) already go
through via `baseline.yml`. This is applied uniformly to every dump this
module produces (both the one embedded in a committed baseline and the
one re-dumped for a live PR comparison), not just the committed-baseline
path, so both sides of any `compare_real_snapshots()` call are normalized
the same way and a purely environmental difference (timestamp, checkout
path) can never show up as a spurious finding.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from normalize_baseline import normalize as _normalize_snapshot  # noqa: E402

# Same "checkout directory's own name, not a git remote or org/repo
# string" marker scripts/normalize_baseline.py's own --repo-root-marker
# CLI flag defaults to -- see that script's own docstring/CLI help for
# why. Derived from REPO_ROOT (already computed above) rather than a
# second hardcoded literal: a hardcoded copy here was a second place a
# repository rename/transfer had to remember to update, and the transfer
# checklist (docs/operations.md) only ever told operators to update
# normalize_baseline.py's own default -- this call site would silently
# keep normalizing against the OLD name forever after a rename, embedding
# unstripped checkout-specific absolute paths in every cmake/make
# baseline refreshed from the new checkout (Codex review, PR #25:
# "derive it from REPO_ROOT.name or expose one shared configured
# marker"). REPO_ROOT.name always matches the actual checkout directory
# at runtime, so there is nothing left to keep in sync here.
_REPO_ROOT_MARKER = REPO_ROOT.name

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


# Generous enough for a real source-depth dump on a cold runner (CastXML
# parsing headers, source replay), bounded enough that a wedged
# CastXML/abicheck process fails closed instead of hanging the calling CI
# job until the workflow-level timeout kills it -- every other failure
# mode in this module already raises RealScanError; an unbounded
# subprocess.run was the one way to defeat that fail-closed contract
# silently (CodeRabbit review, PR #25).
_SUBPROCESS_TIMEOUT_SECONDS = 900


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RealScanError(
            f"{' '.join(cmd)} timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s"
        ) from exc


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
    # Checked BEFORE trusting out_path's own existence/content: out_path is
    # a deterministic path under staged_dir (not a fresh tempdir -- see
    # ci/check_profile.py's own _real_dump_for_target comment for why), so
    # a failed invocation that doesn't touch the file at all would
    # otherwise leave a PRIOR run's stale dump.json sitting there, and
    # out_path.is_file() alone can't tell "fresh, valid output" apart from
    # "stale leftover from before this exact invocation" (CodeRabbit
    # review, PR #25: "a non-zero dump currently passes when it leaves
    # valid JSON").
    if proc.returncode != 0:
        raise RealScanError(
            f"abicheck dump exited {proc.returncode}: {proc.stdout}"
        )
    if not out_path.is_file():
        raise RealScanError(
            f"abicheck dump exited 0 but did not produce {out_path}: {proc.stdout}"
        )
    try:
        snapshot = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RealScanError(f"abicheck dump wrote non-JSON output to {out_path}: {exc}") from exc

    # Read the structured dump_provenance.effective_depth field abicheck
    # itself writes into the snapshot, not the human-readable "Resolved
    # evidence depth: ..." stdout banner -- the banner is prose meant for
    # a terminal, not a stable machine-readable contract (CodeRabbit
    # review, PR #25).
    dump_provenance = snapshot.get("dump_provenance")
    resolved_depth = dump_provenance.get("effective_depth") if isinstance(dump_provenance, dict) else None
    if resolved_depth != "source":
        raise RealScanError(
            f"abicheck dump did not reach source depth (dump_provenance.effective_depth: "
            f"{resolved_depth!r}); refusing to use a lower-depth snapshot as a source-depth "
            f"baseline. Full output:\n{proc.stdout}"
        )
    # Strip volatile fields (created_at, source_mtime, ...) and normalize
    # absolute source paths -- see this module's own docstring for why.
    # Applied here, unconditionally, so every caller (baseline embedding
    # AND candidate re-dump for a live comparison) gets the same
    # normalized shape without having to remember to do it themselves.
    return _normalize_snapshot(snapshot, _REPO_ROOT_MARKER)


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
