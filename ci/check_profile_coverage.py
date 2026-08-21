#!/usr/bin/env python3
"""PR2 item 4: a generalized, profile-aware coverage contract for
`ci/profiles.yaml`'s three build-system profiles -- the multi-build-system
analogue of `scripts/check_coverage_contract.py` (which is Bazel-only, reads
an `abicheck` scan report's `coverage` layers, and stays untouched by this
PR per the task constraints).

## What "coverage" means here, and why it differs per backend

`scripts/check_coverage_contract.py` asks "did `abicheck --depth source`
actually achieve the requested evidence depth" by reading *abicheck's own*
coverage layers out of a scan report. This PR2 gate has no such report to
read for two of its three profiles (CMake/Make never had an `abicheck`
report in the first place -- see `ci/check_profile.py`'s own module
docstring for why) and, even for the Bazel profile, is checking something
narrower and different: "did this profile's *own build* actually produce
the evidence its own `ci/check_profile.py` check needs" -- a candidate
artifact on disk, its declared public headers actually staged, and,
backend-appropriate, some positive signal that this build's compilation
graph was actually captured (Bazel: `bazel cquery` resolved at least one
target; CMake/Make: a `compile_commands.json` with at least one compile
unit). This reuses `check_coverage_contract.py`'s general shape (a `facts`/
`failures`/`gate_status` result, fail-closed on missing evidence) and its
one genuinely transferable piece of logic -- treating "0 things resolved"
as a real coverage gap, not a silently-passing edge case -- rather than
re-deriving that principle from scratch, but the concrete checks below are
new: this script's evidence source is `build-output.json` (PR1's staged
per-profile document), never an `abicheck` report.

## Usage

    ci/check_profile_coverage.py --profile-id ID --staged-dir DIR -o OUT.json [--report TARGET=PATH ...]

`--report target=path` (repeatable, optional) additionally asserts
"evidence identity matches": the report's own `candidate_library_path`
resolves to a file whose freshly-recomputed sha256 equals build-output.json's
own recorded digest for that target -- i.e. the ABI check actually ran
against the exact bytes this profile staged, not a stale or substituted
artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

CI_DIR = Path(__file__).resolve().parent
REPO_ROOT = CI_DIR.parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from select_profiles import load_profiles  # noqa: E402


def _load_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_unit_count(staged_dir: Path, compile_commands_path: Optional[str]) -> Optional[int]:
    if not compile_commands_path:
        return None
    path = staged_dir / compile_commands_path
    doc = _load_json(path)
    if not isinstance(doc, list):
        return None
    return len(doc)


def evaluate(
    profile: Dict[str, Any],
    staged_dir: Path,
    report_paths: Optional[Dict[str, Path]] = None,
) -> Dict[str, Any]:
    coverage_cfg = profile.get("coverage", {})
    checked_targets = coverage_cfg.get("checked_targets", [])
    failures: List[str] = []
    facts: Dict[str, Any] = {
        "profile_id": profile["id"],
        "backend": profile["backend"],
        "checked_targets": checked_targets,
    }

    build_output = _load_json(staged_dir / "build-output.json")
    if build_output is None:
        failures.append(f"no valid build-output.json under {staged_dir}")
        return _result(facts, failures)
    facts["build_success"] = bool(build_output.get("success"))
    if not build_output.get("success"):
        failures.append("profile build did not succeed (build-output.json success=false)")

    # 1. Candidate artifact exists on disk, and its bytes match the digest
    #    build-output.json itself recorded (evidence identity, first half).
    targets = build_output.get("targets", {})
    artifact_facts: Dict[str, Any] = {}
    for target in checked_targets:
        entry = targets.get(target)
        if entry is None:
            failures.append(f"target {target!r} not present in build-output.json at all")
            continue
        if not entry.get("built") or not entry.get("path"):
            failures.append(f"target {target!r} was not built")
            continue
        artifact_path = staged_dir / entry["path"]
        recomputed = _sha256_file(artifact_path)
        artifact_facts[target] = {"path": entry["path"], "recorded_sha256": entry.get("sha256"), "recomputed_sha256": recomputed}
        if recomputed is None:
            failures.append(f"target {target!r} artifact missing on disk: {artifact_path}")
        elif recomputed != entry.get("sha256"):
            failures.append(
                f"target {target!r} staged artifact digest mismatch -- recorded "
                f"{entry.get('sha256')!r}, recomputed {recomputed!r} (artifact changed since staging?)"
            )
    facts["artifacts"] = artifact_facts

    # 2. Public headers actually staged.
    if coverage_cfg.get("require_public_headers", True):
        header_roots = build_output.get("header_roots", [])
        facts["header_roots_staged"] = header_roots
        if not header_roots:
            failures.append("no header_roots staged -- build-output.json's own header_roots list is empty")
        else:
            for root in header_roots:
                root_dir = staged_dir / root
                files = [p for p in root_dir.rglob("*")] if root_dir.is_dir() else []
                header_files = [p for p in files if p.is_file()]
                if not header_files:
                    failures.append(f"header root {root!r} staged but contains no files")

    # 3. Backend-appropriate compile-evidence signal.
    backend_evidence = build_output.get("evidence", {}).get("backend_evidence", {})
    backend = profile["backend"]
    if backend == "bazel":
        min_resolved = coverage_cfg.get("min_resolved_targets", 1)
        resolved_targets = backend_evidence.get("targets", [])
        facts["resolved_target_count"] = len(resolved_targets)
        if backend_evidence.get("kind") != "bazel-cquery":
            failures.append(f"expected bazel-cquery evidence, got kind={backend_evidence.get('kind')!r}")
        elif len(resolved_targets) < min_resolved:
            failures.append(
                f"resolved_target_count {len(resolved_targets)} < required minimum {min_resolved}"
            )
    else:  # cmake, make
        min_units = coverage_cfg.get("min_compile_units", 1)
        require_cc = coverage_cfg.get("require_compile_commands", True)
        present = backend_evidence.get("compile_commands_present", False)
        facts["compile_commands_present"] = present
        if not present:
            if require_cc:
                failures.append("compile_commands.json was not generated for this profile (required)")
            else:
                facts["compile_commands_note"] = (
                    "compile_commands.json not generated -- not required for this profile "
                    "(see ci/profiles.yaml's coverage.require_compile_commands)"
                )
        else:
            count = _compile_unit_count(staged_dir, backend_evidence.get("compile_commands_path"))
            facts["compile_unit_count"] = count
            if count is None:
                failures.append("compile_commands.json present but unreadable/not a JSON array")
            elif count < min_units:
                failures.append(f"compile_unit_count {count} < required minimum {min_units}")

    # 4. Evidence identity: the ABI report (if supplied) actually ran
    #    against these exact staged bytes.
    if report_paths:
        report_facts: Dict[str, Any] = {}
        for target, report_path in report_paths.items():
            report = _load_json(report_path)
            if report is None:
                failures.append(f"report for target {target!r} at {report_path} is missing/invalid JSON")
                continue
            candidate_rel = report.get("candidate_library_path")
            target_facts = artifact_facts.get(target)
            report_facts[target] = {"report_path": str(report_path), "candidate_library_path": candidate_rel}
            if report.get("verdict") == "NOT_COMPARABLE":
                # Nothing to cross-check -- a NOT_COMPARABLE report never
                # resolved a candidate library path at all.
                continue
            if not candidate_rel or target_facts is None:
                failures.append(f"report for target {target!r} has no candidate_library_path to cross-check")
                continue
            expected_rel = target_facts["path"]
            if candidate_rel != expected_rel:
                failures.append(
                    f"report for target {target!r} checked {candidate_rel!r}, but "
                    f"build-output.json staged this target at {expected_rel!r}"
                )
        facts["report_cross_check"] = report_facts

    return _result(facts, failures)


def _result(facts: Dict[str, Any], failures: List[str]) -> Dict[str, Any]:
    passed = not failures
    return {
        "facts": facts,
        "failures": failures,
        "gate_status": "PASS" if passed else "FAIL",
        "analysis_status": "COMPLETE" if passed else "INCOMPLETE",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--staged-dir", type=Path, required=True)
    parser.add_argument("--profiles-file", type=Path, default=CI_DIR / "profiles.yaml")
    parser.add_argument("--report", action="append", default=[], help="target=path, repeatable")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)

    profiles = load_profiles(args.profiles_file)
    if args.profile_id not in profiles:
        print(f"check_profile_coverage: unknown profile id {args.profile_id!r}", file=sys.stderr)
        return 1
    profile = profiles[args.profile_id]

    report_paths: Dict[str, Path] = {}
    for pair in args.report:
        if "=" not in pair:
            print(f"check_profile_coverage: --report must be target=path, got {pair!r}", file=sys.stderr)
            return 1
        target, _, path = pair.partition("=")
        report_paths[target] = Path(path)

    result = evaluate(profile, args.staged_dir, report_paths or None)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")

    if result["gate_status"] != "PASS":
        for failure in result["failures"]:
            print(f"::error::coverage contract: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
