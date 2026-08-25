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

from check_profile import CheckProfileError, _load_build_output  # noqa: E402
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

    try:
        build_output = _load_build_output(staged_dir)
    except (CheckProfileError, OSError, json.JSONDecodeError):
        build_output = None
    # A syntactically valid but non-object document (e.g. `[]`) is truthy
    # and not None, so "is None" alone let it through to every .get() call
    # below, each raising AttributeError (CodeRabbit review, PR #20; same
    # shape as the report-JSON hardening already applied elsewhere in this
    # PR -- see ci/emit_profile_receipt.py's own comment on this exact
    # pattern).
    if not isinstance(build_output, dict):
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
                # Short-circuits on the first file found instead of
                # materializing the whole tree just to prove one file
                # exists (CodeRabbit review, PR #20).
                has_file = root_dir.is_dir() and any(p.is_file() for p in root_dir.rglob("*"))
                if not has_file:
                    failures.append(f"header root {root!r} staged but contains no files")

    # 3. Backend-appropriate compile-evidence signal.
    # `evidence`/`backend_evidence` are themselves attacker/producer-
    # controlled JSON, not guaranteed to be objects even if build_output
    # itself is one (e.g. `"evidence": null` from a degraded build) --
    # coerce each to {} before chaining another .get() onto it, same
    # reasoning as the build_output guard above.
    evidence = build_output.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    backend_evidence = evidence.get("backend_evidence")
    if not isinstance(backend_evidence, dict):
        backend_evidence = {}
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
            # backend_evidence.get("note", "") only substitutes the default
            # when the key is absent -- an explicit "note": null still
            # returns None, and None.startswith() below would raise
            # (CodeRabbit review, PR #20).
            note = backend_evidence.get("note") or ""
            # require_compile_commands: false exists specifically to
            # tolerate a runner that never had bear installed (make.py's
            # documented best-effort posture for that case) -- it was not
            # meant to also swallow a genuine invocation *failure* on a
            # runner where bear IS installed (integration-shadow.yml
            # explicitly installs it), which is a real signal something's
            # broken, not an absent-optional-tool no-op (Codex review,
            # PR #20). make.py's own note text distinguishes the two.
            tool_absent = note.startswith("bear not installed")
            if require_cc or not tool_absent:
                failures.append(
                    "compile_commands.json was not generated for this profile "
                    f"({'required' if require_cc else 'bear was expected to be installed'}): {note}"
                )
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
    #
    # Every target ci/profiles.yaml's coverage.checked_targets declares
    # must have a report -- this previously only validated whichever
    # --report flags were actually passed on the command line, with no
    # cross-check against checked_targets itself. A profile that adds or
    # renames a checked_targets entry without also updating the workflow's
    # --report flags would then have that target's artifact staged and
    # "covered" by every other check here, but receive no ABI comparison
    # at all -- and this contract, plus the receipt/gate built on it,
    # would still pass (Codex review, PR #20).
    # Only enforced when the caller opted into report-based checking at
    # all (report_paths is not None) -- an evidence-only coverage check
    # (report_paths omitted entirely) is a distinct, legitimate mode this
    # function has always supported and must keep supporting.
    if report_paths is not None:
        missing_reports = sorted(set(checked_targets) - set(report_paths))
        for target in missing_reports:
            failures.append(f"checked_targets includes {target!r} but no --report was supplied for it")

    if report_paths:
        report_facts: Dict[str, Any] = {}
        for target, report_path in report_paths.items():
            report = _load_json(report_path)
            # Same non-object-JSON hazard as build_output above -- a
            # syntactically valid `[]`/`null`/string report is truthy and
            # not None, so "is None" alone let it reach report.get() below
            # and raise AttributeError instead of routing through this
            # existing missing/invalid failure (CodeRabbit review, PR #20).
            if not isinstance(report, dict):
                failures.append(f"report for target {target!r} at {report_path} is missing/invalid JSON")
                continue
            candidate_rel = report.get("candidate_library_path")
            candidate_sha256 = report.get("candidate_library_sha256")
            target_facts = artifact_facts.get(target)
            report_facts[target] = {
                "report_path": str(report_path),
                "candidate_library_path": candidate_rel,
                "candidate_library_sha256": candidate_sha256,
                "report_profile_id": report.get("profile_id"),
                "report_target": report.get("target"),
            }
            # The report's own profile_id/target identity must match what
            # we're actually evaluating -- otherwise a stale report from a
            # different profile or an earlier build could still pass this
            # cross-check purely because every profile stages targets at
            # the same relative path (e.g. artifacts/lib/libmath.so), which
            # candidate_library_path alone can't distinguish (Codex review,
            # PR #20).
            if report.get("profile_id") != profile["id"]:
                failures.append(
                    f"report for target {target!r} was produced for profile "
                    f"{report.get('profile_id')!r}, not the profile being "
                    f"evaluated ({profile['id']!r})"
                )
                continue
            if report.get("target") != target:
                failures.append(
                    f"report at {report_path} was produced for target "
                    f"{report.get('target')!r}, not the target it was supplied for ({target!r})"
                )
                continue
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
                continue
            # Path identity alone isn't evidence identity: every profile
            # always stages a given target at the same relative path
            # across rebuilds, so a stale report from an earlier build of
            # this exact profile/target -- already ruled comparable by the
            # identity checks above -- could still carry a clean verdict
            # for bytes that no longer exist at that path. Binding the
            # report's own recorded candidate digest to the artifact's
            # independently recomputed one (artifact_facts, above) is what
            # actually proves this report analyzed THESE bytes, not merely
            # a report that once looked at this path (Codex review, PR #20;
            # follow-up to the profile_id/target identity fix above --
            # check_profile.py now records candidate_library_sha256 for
            # exactly this purpose).
            expected_sha256 = target_facts.get("recomputed_sha256")
            if not candidate_sha256:
                failures.append(
                    f"report for target {target!r} has no candidate_library_sha256 to cross-check "
                    "(stale report from before this field existed?)"
                )
            elif expected_sha256 is None or candidate_sha256 != expected_sha256:
                failures.append(
                    f"report for target {target!r} analyzed digest {candidate_sha256!r}, but "
                    f"the staged artifact at {candidate_rel!r} now recomputes to {expected_sha256!r} -- "
                    "stale report, not evidence for the current build"
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
