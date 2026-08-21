#!/usr/bin/env python3
"""PR2 item 2: write one per-profile integration receipt (schema
`abicheck.integration-profile-receipt/v1`,
`ci/schemas/profile-receipt.schema.json`) after a profile's build + ABI
check (`ci/check_profile.py`) + coverage contract
(`ci/check_profile_coverage.py`) have all run.

Mirrors `scripts/emit_capability_receipt.py`/`scripts/capability_receipts.py`'s
own `passed`/`failed`/`skipped` receipt contract deliberately -- this is the
same "was it actually executed and did it pass" evidence for the new
integration gate that those scripts already provide for the existing Bazel
gate's `gating: true` capabilities. It is a distinct schema/file, not an
extension of that one: `capability_receipts.py`'s schema is fixed to one
`capability_id` string with no report/coverage detail, and this PR2 gate's
receipts need to carry per-target verdicts, a build-output digest, and
scanner/build-system identity that schema has no room for -- see
`ci/schemas/profile-receipt.schema.json`'s own header comment.

Usage (from a workflow `run:` step, one call per profile after that
profile's checks have all run):

    python3 ci/emit_profile_receipt.py \\
      --profile-id linux-x86_64-gcc14-cxx17-cmake-ninja \\
      --staged-dir abicheck-build-linux-x86_64-gcc14-cxx17-cmake-ninja \\
      --report math=/tmp/report-math.json \\
      --report strings=/tmp/report-strings.json \\
      --coverage-result /tmp/coverage-cmake.json \\
      --workflow integration-shadow.yml --job build \\
      --run-id "$GITHUB_RUN_ID" --run-attempt "$GITHUB_RUN_ATTEMPT" --sha "$GITHUB_SHA" \\
      --out "$RUNNER_TEMP/profile-receipts/linux-x86_64-gcc14-cxx17-cmake-ninja.json"

`--report target=path` is repeatable; a target whose report file is missing
or unreadable is recorded with verdict `NOT_RUN` (never silently omitted --
see the module-level VALID_VERDICTS handling below) so a report that failed
to be produced at all still shows up as a receipt-level failure, the same
fail-closed contract `capability_receipts.py`'s own module docstring
describes for a missing gating signal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
KIND = "abicheck.integration-profile-receipt/v1"

# What a report's own verdict may legally be (ci/check_profile.py's set,
# plus NOT_RUN for "no report file at all" -- distinct from NOT_COMPARABLE,
# which is a real result check_profile.py itself can produce when the
# candidate/baseline couldn't be compared; NOT_RUN means this receipt never
# even got a report to read).
_FAILING_VERDICTS = frozenset({"BREAKING", "NOT_COMPARABLE", "NOT_RUN"})


class ReceiptEmitError(RuntimeError):
    pass


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _parse_report_args(pairs: List[str]) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ReceiptEmitError(f"--report must be target=path, got {pair!r}")
        target, _, path = pair.partition("=")
        if not target:
            raise ReceiptEmitError(f"--report target must be non-empty: {pair!r}")
        result[target] = Path(path)
    return result


def build_reports(report_paths: Dict[str, Path]) -> List[Dict[str, Any]]:
    reports = []
    for target in sorted(report_paths):
        path = report_paths[target]
        digest = _sha256_file(path)
        doc = _load_json(path) if digest else None
        # A report file can be valid JSON with a non-object top level (e.g.
        # a truncated/corrupted write leaving `[]` or `null`) -- doc.get()
        # on that raises AttributeError instead of falling through to
        # NOT_RUN, which crashes this always-running receipt step and
        # loses the per-target failure detail the receipt is supposed to
        # capture (Codex review, PR #20).
        verdict = doc.get("verdict") if isinstance(doc, dict) else "NOT_RUN"
        if verdict not in ("NO_CHANGE", "COMPATIBLE", "BREAKING", "NOT_COMPARABLE", "NOT_RUN"):
            verdict = "NOT_RUN"
        reports.append({
            "target": target,
            "path": str(path) if digest else None,
            "digest": digest,
            "verdict": verdict,
        })
    return reports


def build_receipt(
    *,
    profile_id: str,
    staged_dir: Path,
    report_paths: Dict[str, Path],
    coverage_result: Optional[Path],
    workflow: str,
    job: str,
    run_id: str,
    run_attempt: str,
    sha: str,
    required: bool,
) -> Dict[str, Any]:
    build_output_path = staged_dir / "build-output.json"
    build_output = _load_json(build_output_path)
    # A failed/corrupted profile run can leave syntactically valid JSON with
    # a non-object top level (e.g. `[]`) at build-output.json -- that's
    # truthy and not None, so every .get() call below would raise
    # AttributeError instead of falling through to the "no valid
    # build-output.json" status this function already handles for a
    # missing/unparseable file. Since this emitter runs under `if: always()`
    # specifically to record failed runs, that crash would drop the failed
    # receipt entirely and lose the integration gate's build diagnostics
    # (Codex review, PR #20; same shape as build_reports()'s report-JSON fix
    # above).
    if not isinstance(build_output, dict):
        build_output = None
    build_output_digest = _sha256_file(build_output_path)

    reports = build_reports(report_paths)

    coverage_doc = _load_json(coverage_result) if coverage_result else None
    if not isinstance(coverage_doc, dict):
        coverage_doc = None

    detail_parts = []
    status = "passed"

    if build_output is None:
        status = "failed"
        detail_parts.append(f"no valid build-output.json at {build_output_path}")
    elif not build_output.get("success"):
        status = "failed"
        detail_parts.append("profile build did not succeed (see build-output.json diagnostics)")

    failing_reports = [r["target"] for r in reports if r["verdict"] in _FAILING_VERDICTS]
    if failing_reports:
        status = "failed"
        detail_parts.append(f"target(s) with a failing verdict: {', '.join(failing_reports)}")
    if not reports:
        status = "failed"
        detail_parts.append("no --report entries supplied -- nothing was checked for this profile")

    if coverage_result is not None:
        if coverage_doc is None:
            status = "failed"
            detail_parts.append(f"coverage result at {coverage_result} is missing or invalid JSON")
        elif coverage_doc.get("gate_status") != "PASS":
            status = "failed"
            detail_parts.append("coverage contract did not pass (see coverage result failures)")

    backend = (build_output or {}).get("profile", {}).get("backend", "unknown")
    compiler = (build_output or {}).get("compiler", {})

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "profile_id": profile_id,
        "required": bool(required),
        "status": status,
        "workflow": workflow,
        "job": job,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "sha": sha,
        "build": {"success": bool(build_output.get("success")) if build_output is not None else False},
        "scanner": {
            "mechanism": "nm/readelf dynamic-symbol-table + public-header-digest diff (ci/check_profile.py) -- see UPSTREAM_TO_ABICHECK.md's PR2 entry for what this simplifies versus the full abicheck scanner",
        },
        "build_system": backend,
        "compiler": compiler,
        "build_output_digest": build_output_digest,
        "reports": reports,
        "coverage": coverage_doc,
        "detail": "; ".join(detail_parts),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--staged-dir", type=Path, required=True)
    parser.add_argument("--report", action="append", default=[], help="target=path, repeatable")
    parser.add_argument("--coverage-result", type=Path, default=None)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--run-attempt", default="0")
    parser.add_argument("--sha", default="unknown")
    parser.add_argument("--required", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        report_paths = _parse_report_args(args.report)
        receipt = build_receipt(
            profile_id=args.profile_id,
            staged_dir=args.staged_dir,
            report_paths=report_paths,
            coverage_result=args.coverage_result,
            workflow=args.workflow,
            job=args.job,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            sha=args.sha,
            required=args.required,
        )
    except ReceiptEmitError as exc:
        print(f"emit_profile_receipt: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"emit_profile_receipt: wrote {args.out} (status={receipt['status']})")
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
