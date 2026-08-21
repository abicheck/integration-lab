#!/usr/bin/env python3
"""PR2 item 3: the final `integration_gate` job's own logic --
`.github/workflows/integration-shadow.yml` calls this after every profile's
build+check+receipt steps have run, downloading each profile's receipt
(`ci/emit_profile_receipt.py`) into one directory first.

Validates, for every profile the `select` job's own matrix declared this run
(the same expected set `ci/select_profiles.py` already resolved -- passed in
via `--expected-profile`, repeatable):

- a receipt file exists at `<receipts-dir>/<profile-id>.json`;
- it validates against `ci/schemas/profile-receipt.schema.json` (reusing
  `ci/validate_build_output.py`'s own schema-validation helper -- `jsonschema`
  when available, the same dependency-light structural fallback otherwise --
  rather than a second, duplicate validator);
- its own `status` is `passed` (a `failed` receipt, or one that never
  resolved to `passed`/`failed` at all, fails this profile's row).

Writes a JSON gate result (`-o`) and prints a Markdown
`profile | build | evidence | verdict | status` table to stdout, appendable
straight to `$GITHUB_STEP_SUMMARY`. Exit code is non-zero the moment any
expected profile is missing, invalid, or failed -- this is a REAL gate (see
this PR's own README/UPSTREAM_TO_ABICHECK.md entries for why it stays
advisory rather than required), not a renderer that always reports success.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

CI_DIR = Path(__file__).resolve().parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from validate_build_output import validate_document  # noqa: E402

DEFAULT_SCHEMA = CI_DIR / "schemas" / "profile-receipt.schema.json"


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _verdict_symbol(verdict: str) -> str:
    return {
        "NO_CHANGE": "✅ NO_CHANGE",
        "COMPATIBLE": "✅ COMPATIBLE",
        "BREAKING": "❌ BREAKING",
        "NOT_COMPARABLE": "⚠️ NOT_COMPARABLE",
        "NOT_RUN": "⚠️ NOT_RUN",
    }.get(verdict, f"? {verdict}")


def evaluate(
    expected_profiles: List[str],
    receipts_dir: Path,
    schema_path: Path = DEFAULT_SCHEMA,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    failures: List[str] = []

    for profile_id in sorted(expected_profiles):
        receipt_path = receipts_dir / f"{profile_id}.json"
        row: Dict[str, Any] = {"profile_id": profile_id}
        if not receipt_path.is_file():
            failures.append(f"{profile_id}: no receipt found at {receipt_path}")
            rows.append({**row, "status": "MISSING", "build": None, "reports": [], "gate_status": "FAIL"})
            continue

        try:
            receipt = _load_json(receipt_path)
        # UnicodeDecodeError (a binary/truncated artifact download) is a
        # ValueError, not a JSONDecodeError, so it escaped this handler
        # entirely; OSError (permission, an unexpected directory at this
        # path) escaped it too. Either crashed this whole gate job with a
        # traceback instead of the readable INVALID row this branch exists
        # to produce -- exactly the corrupted-artifact case this gate is
        # meant to catch (CodeRabbit review, PR #20).
        except (OSError, ValueError) as exc:
            failures.append(f"{profile_id}: receipt is not valid JSON ({exc})")
            rows.append({**row, "status": "INVALID", "build": None, "reports": [], "gate_status": "FAIL"})
            continue

        schema_errors = validate_document(receipt, schema_path)
        if schema_errors:
            failures.append(f"{profile_id}: receipt fails schema validation: {'; '.join(schema_errors[:5])}")
            # receipt is valid JSON but not necessarily an object here --
            # validate_document() flags e.g. a top-level `[]` as a schema
            # error too, and calling .get() on that crashed this whole
            # job with an unhandled AttributeError instead of emitting the
            # SCHEMA_INVALID row/failure this branch promises (Codex
            # review, PR #20).
            receipt_dict = receipt if isinstance(receipt, dict) else {}
            rows.append({**row, "status": "SCHEMA_INVALID", "build": receipt_dict.get("build"), "reports": receipt_dict.get("reports", []), "gate_status": "FAIL"})
            continue

        # The schema only requires a nonempty profile_id -- it says nothing
        # about which profile that string names. A receipt at the expected
        # filename (<receipts-dir>/<profile-id>.json) can still be a
        # schema-valid, passed receipt for a *different* profile (a copied
        # or misnamed artifact download, e.g. from PR1's per-profile
        # artifact naming) -- this loop was keying entirely off the
        # filename/status/verdicts and would report PASS for the expected
        # profile with zero evidence it was ever actually checked (Codex
        # review, PR #20).
        if receipt.get("profile_id") != profile_id:
            failures.append(
                f"{profile_id}: receipt at {receipt_path} is for profile_id="
                f"{receipt.get('profile_id')!r}, not {profile_id!r} -- wrong artifact?"
            )
            rows.append({**row, "status": "WRONG_PROFILE", "build": receipt.get("build"), "reports": receipt.get("reports", []), "gate_status": "FAIL"})
            continue

        status = receipt.get("status")
        reports = receipt.get("reports", [])
        breaking_or_missing = [r for r in reports if r.get("verdict") in ("BREAKING", "NOT_COMPARABLE", "NOT_RUN")]
        if status != "passed":
            failures.append(f"{profile_id}: receipt status={status!r} ({receipt.get('detail', '')})")
        elif breaking_or_missing:
            failures.append(
                f"{profile_id}: receipt status=passed but has non-clean report verdict(s): "
                + ", ".join(f"{r['target']}={r['verdict']}" for r in breaking_or_missing)
            )

        # The schema requires build.success and permits any coverage
        # shape, but constrains neither against `status` -- a schema-valid
        # receipt claiming status="passed" while build.success is false or
        # coverage.gate_status is "FAIL" would otherwise pass this loop
        # purely on the producer's own say-so, with the actual evidence it
        # carries contradicting that claim. ci/emit_profile_receipt.py's
        # own build_receipt() always derives status FROM these facts, so
        # this can't happen from that emitter today -- but this consumer
        # reads an arbitrary JSON file, not a guaranteed call into that
        # function, so trusting the aggregate status alone over the
        # underlying facts it's supposed to summarize is the same
        # single-field-of-trust gap already closed elsewhere in this PR
        # (Codex review, PR #20).
        build_evidence = receipt.get("build")
        build_success = isinstance(build_evidence, dict) and build_evidence.get("success") is True
        if status == "passed" and not build_success:
            failures.append(f"{profile_id}: receipt status=passed but build.success is not true: {build_evidence!r}")
        coverage_evidence = receipt.get("coverage")
        coverage_failed = isinstance(coverage_evidence, dict) and coverage_evidence.get("gate_status") not in (None, "PASS")
        if status == "passed" and coverage_failed:
            failures.append(
                f"{profile_id}: receipt status=passed but coverage.gate_status="
                f"{coverage_evidence.get('gate_status')!r}"
            )

        row_status = "passed" if status == "passed" and not breaking_or_missing and build_success and not coverage_failed else "failed"
        rows.append({
            **row,
            "status": row_status,
            "build": receipt.get("build"),
            "reports": reports,
            "gate_status": "PASS" if row_status == "passed" else "FAIL",
        })

    overall = "PASS" if not failures else "FAIL"
    return {"expected_profiles": sorted(expected_profiles), "rows": rows, "failures": failures, "gate_status": overall}


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "### Integration gate (PR2, advisory -- not a required check)",
        "",
        f"Overall: **{result['gate_status']}**",
        "",
        "| Profile | Build | Evidence | Verdict | Status |",
        "|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        build = row.get("build") or {}
        build_cell = "✅ built" if build.get("success") else "❌ build failed" if row["status"] not in ("MISSING",) else "n/a"
        reports = row.get("reports") or []
        evidence_cell = ", ".join(sorted({r.get("target", "?") for r in reports})) or "n/a"
        verdict_cell = "<br>".join(f"{r.get('target')}: {_verdict_symbol(r.get('verdict', '?'))}" for r in reports) or "n/a"
        status_cell = "✅ passed" if row["status"] == "passed" else f"❌ {row['status']}"
        lines.append(f"| `{row['profile_id']}` | {build_cell} | {evidence_cell} | {verdict_cell} | {status_cell} |")
    lines.append("")
    if result["failures"]:
        lines.append("**Failures:**")
        lines.append("")
        for failure in result["failures"]:
            lines.append(f"- {failure}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--receipts-dir", type=Path, required=True)
    parser.add_argument("--expected-profile", action="append", default=[], dest="expected_profiles", required=True)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)

    result = evaluate(args.expected_profiles, args.receipts_dir, args.schema)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(render_markdown(result))

    return 0 if result["gate_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
