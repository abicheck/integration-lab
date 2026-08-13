#!/usr/bin/env python3
"""Render a short, canonical PR-comment body from an ABICheck `scan` JSON report.

Why this exists: abicheck's built-in sticky PR-comment renderer
(`pr-comment: true`) only activates for `mode: compare` (see
`action/run.sh`'s `_maybe_post_pr_comment`, gated on `case "$MODE" in
compare) ;; *) return 0 ;; esac`) -- it is a silent no-op for `mode: scan`,
in both the last released action (v0.5.0) and current `abicheck/main`. So a
workflow that runs a single canonical `scan` as its gate has no built-in way
to put that scan's own verdict on the PR.

This script fills that gap with a deliberately small, literal rendering of
the fields `ScanOutcome.to_dict()` actually emits (verified against
abicheck v0.5.0's `abicheck/scan_engine.py`): `verdict`, `exit_code`,
`level.depth`, `coverage` (presence/count only -- the per-entry schema
varies by layer and isn't asserted on here), and `advisories` (the
human-readable evidence-gap notices, e.g. "Macros, default args,
inline/template/constexpr bodies -- off"). It intentionally does NOT
attempt to re-derive a pass/fail verdict from nested report internals --
that authority stays with abicheck's own `verdict`/`exit_code`.

The one addition to that principle: `--coverage-contract`, the result of
`scripts/check_coverage_contract.py` (a second, independent, lab-side gate
-- see that script's docstring for why it exists). Its result is shown as
its own clearly-labeled section, never merged into or silently overriding
abicheck's own verdict line above it -- both facts stay visible and
attributed to the analysis that produced them.
"""
import argparse
import json
import sys

MARKER = "<!-- abicheck-lab-canonical-report -->"

_VERDICT_LINES = {
    "COMPATIBLE": "✅ **COMPATIBLE** — no gated incompatibility detected.",
    "COMPATIBLE_WITH_RISK": "⚠️ **COMPATIBLE_WITH_RISK** — no gated incompatibility, but a non-gating risk finding was recorded.",
    "SEVERITY_ERROR": "⚠️ **SEVERITY_ERROR** — a severity-level issue was detected.",
    "API_BREAK": "🛑 **API_BREAK** — a source-level API break was detected.",
    "BREAKING": "🛑 **BREAKING** — a binary ABI break was detected.",
    "COVERAGE_INCOMPLETE": "⚠️ **COVERAGE_INCOMPLETE** — evidence coverage was insufficient to fully evaluate this change.",
    "BUDGET_OVERFLOW": "⏱️ **BUDGET_OVERFLOW** — the scan exceeded its configured budget.",
    "ERROR": "🛑 **ERROR** — abicheck encountered an error.",
}


def _render_coverage_contract(contract):
    if contract is None:
        return []
    lines = ["---", ""]
    status = contract.get("gate_status")
    if status == "PASS":
        lines.append(
            f"✅ **Coverage contract: satisfied** — `depth: {contract.get('requested_depth')}` "
            "evidence met the minimum requirements (Bazel target resolved, "
            "export-to-source linkage, public-header provenance)."
        )
    elif status == "UNKNOWN":
        lines.append(
            "⚠️ **Coverage contract: result unavailable** — the gate step ran but "
            "its result couldn't be read here. Treat this the same as a failed "
            "contract: do not assume it passed."
        )
        lines.append("")
        for failure in contract.get("failures", []):
            lines.append(f"- {failure}")
    else:
        lines.append(
            f"🛑 **Coverage contract: NOT satisfied** — `analysis_status: "
            f"{contract.get('analysis_status')}`, `compatibility_verdict: "
            f"{contract.get('compatibility_verdict')}`. This gates independently of "
            "the abicheck verdict above: even a COMPATIBLE result isn't trusted "
            "when the requested depth wasn't actually achieved."
        )
        lines.append("")
        for failure in contract.get("failures", []):
            lines.append(f"- {failure}")
    facts = contract.get("facts", {})
    if facts:
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Coverage facts</summary>")
        lines.append("")
        lines.append("| Fact | Value |")
        lines.append("|------|-------|")
        for key, value in facts.items():
            lines.append(f"| `{key}` | `{value}` |")
        lines.append("")
        lines.append("</details>")
    lines.append("")
    return lines


def render(report, *, base_sha, head_sha, requested_depth, run_url, artifact_note, coverage_contract=None):
    verdict = report.get("verdict", "UNKNOWN")
    exit_code = report.get("exit_code")
    level = report.get("level", {}) or {}
    effective_depth = level.get("depth")
    coverage = report.get("coverage", []) or []
    advisories = report.get("advisories", []) or []

    lines = [MARKER, "## ABICheck source scan"]
    lines.append("")
    lines.append(_VERDICT_LINES.get(verdict, f"**{verdict}**"))
    lines.append("")
    lines.append("| Property | Value |")
    lines.append("|----------|-------|")
    lines.append(f"| Requested depth | `{requested_depth}` |")
    lines.append(f"| Effective depth | `{effective_depth}` |")
    lines.append(f"| Exit code | `{exit_code}` |")
    lines.append(f"| Coverage entries | {len(coverage)} (see full report for detail) |")
    lines.append(f"| Base SHA | `{base_sha}` |")
    lines.append(f"| Head SHA | `{head_sha}` |")
    lines.append("")

    if requested_depth and effective_depth and requested_depth != effective_depth:
        lines.append(
            f"> ⚠️ Requested `depth: {requested_depth}` but the scan's effective "
            f"depth was `{effective_depth}` — evidence for the requested depth "
            "was not fully available; treat this result as narrower than requested."
        )
        lines.append("")

    if advisories:
        lines.append("<details>")
        lines.append("<summary>Evidence-gap advisories</summary>")
        lines.append("")
        for advisory in advisories:
            lines.append(f"- {advisory}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.extend(_render_coverage_contract(coverage_contract))

    if run_url:
        lines.append(f"[Full report / artifacts]({run_url})")
    if artifact_note:
        lines.append("")
        lines.append(artifact_note)

    lines.append("")
    lines.append(
        "_This comment is generated from the same canonical `abicheck scan` "
        "run that gates this PR — not a separate analysis._"
    )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="Path to the scan JSON report (may be missing/empty on a hard tool error)")
    parser.add_argument("-o", "--output", required=True, help="Path to write the rendered comment markdown")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--requested-depth", default="source")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--artifact-note", default="")
    parser.add_argument(
        "--coverage-contract", default="",
        help="Path to scripts/check_coverage_contract.py's output JSON (optional)",
    )
    args = parser.parse_args()

    try:
        with open(args.report, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        report = None
        load_error = str(exc)
    else:
        load_error = None

    coverage_contract = None
    if args.coverage_contract:
        try:
            with open(args.coverage_contract, "r", encoding="utf-8") as fh:
                coverage_contract = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            # A caller that passes --coverage-contract is asserting the
            # gate ran; silently dropping the section here would show a
            # green-looking comment with the second gate's result just
            # missing -- readers can't tell "gate passed" from "gate
            # result unknown" (CodeRabbit review). Render it as an
            # explicit unknown/failed state instead of omitting it.
            coverage_contract = {
                "gate_status": "UNKNOWN",
                "analysis_status": "UNKNOWN",
                "compatibility_verdict": "NOT_FULLY_EVALUATED",
                "requested_depth": args.requested_depth,
                "failures": [f"coverage contract result unreadable: {exc}"],
                "facts": {},
            }

    if report is None:
        body = (
            f"{MARKER}\n## ABICheck source scan\n\n"
            f"🛑 **ERROR** — no readable JSON report was produced ({load_error}).\n\n"
            f"| Base SHA | `{args.base_sha}` |\n|---|---|\n| Head SHA | `{args.head_sha}` |\n\n"
        )
        if args.run_url:
            body += f"[Workflow run]({args.run_url})\n"
    else:
        body = render(
            report,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            requested_depth=args.requested_depth,
            run_url=args.run_url,
            artifact_note=args.artifact_note,
            coverage_contract=coverage_contract,
        )

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(body)


if __name__ == "__main__":
    sys.exit(main())
