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

Findings: `scan --against`'s `diff` block (`cli_scan_baseline._run_baseline_compare`'s
`summary`) carries the actual per-symbol findings behind the verdict --
`diff.findings` (breaking/api_break/risk/not_evaluated, each a dict with
`kind`/`symbol`/`description`/`source_location`), plus the always-on
`diff.additions`/`diff.quality` (compatible-but-itemized surface changes)
and `diff.suppressed`/`diff.suppressed_count`. Before this, the comment
only ever showed the aggregate verdict (e.g. `COMPATIBLE_WITH_RISK`) with
no way to tell *what* was found -- a coverage-complete PR could read as
"something is risky" with zero indication of what, where, or why. This
renders the same findings the full JSON report already carries, not a
new analysis.
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


_BUCKET_LABELS = {
    "breaking": "🛑 Breaking (binary ABI)",
    "api_break": "🛑 API break (source)",
    "risk": "⚠️ Risk",
    "compatible": "✅ Compatible (gating cause only, if any)",
    "not_evaluated": "➖ Not evaluated (contract-excluded)",
}

#: Cap on how many individual findings this renders per section -- the full
#: list always still lives in the abicheck-report artifact; this comment is
#: a summary, not a mirror of the JSON (matches diff.findings_truncated's
#: own cap philosophy, just applied a second time for comment brevity).
_MAX_RENDERED_FINDINGS = 15


def _finding_line(entry):
    kind = entry.get("kind") or "?"
    symbol = entry.get("symbol") or "?"
    description = entry.get("description")
    loc = entry.get("source_location")
    line = f"- `{kind}` — `{symbol}`"
    if description:
        line += f" — {description}"
    if loc:
        line += f" ({loc})"
    return line


def _render_findings(diff):
    """Render the actual per-symbol findings behind ``diff``'s bucket counts.

    ``diff`` is ``report.get("diff")`` -- the ``scan --against`` summary
    dict (see this module's own docstring for the exact schema). ``None``
    or empty when the scan never ran a baseline compare (no ``--against``,
    or a hard error before the compare stage) -- in that case this renders
    nothing, same as before this function existed.
    """
    if not diff or not isinstance(diff, dict):
        return []

    lines = ["---", "", "### Findings", ""]

    counts = {
        bucket: diff.get(bucket)
        for bucket in ("breaking", "api_break", "risk", "compatible")
        if diff.get(bucket) is not None
    }
    if counts:
        lines.append("| Bucket | Count |")
        lines.append("|--------|------:|")
        for bucket, count in counts.items():
            label = _BUCKET_LABELS.get(bucket, bucket)
            lines.append(f"| {label} | {count} |")
        lines.append("")

    findings = diff.get("findings") or []
    if findings:
        shown, rest = findings[:_MAX_RENDERED_FINDINGS], findings[_MAX_RENDERED_FINDINGS:]
        for entry in shown:
            lines.append(_finding_line(entry))
        # Two independent truncation points, reported separately (Codex
        # review): `rest` is only what THIS renderer additionally cut, on
        # top of whatever abicheck's own `scan --against` summary already
        # capped `diff.findings` to upstream (`findings_truncated` +
        # `findings_truncated_kinds`, a kind -> cut-count map -- see
        # `cli_scan_baseline._baseline_finding_dicts`'s docstring). If the
        # upstream cap and this renderer's cap happen to coincide (e.g.
        # both discard nothing further because the report already has
        # exactly `_MAX_RENDERED_FINDINGS` entries), `rest` is empty but
        # `findings_truncated` can still be true -- collapsing the two
        # into one count would silently understate (or, before this fix,
        # completely hide as "+0 more") what abicheck itself already
        # discarded before this script ever saw the report.
        if rest:
            lines.append(
                f"- _(+{len(rest)} more finding(s) not shown here (this comment's "
                "own display cap) -- see the full abicheck-report artifact.)_"
            )
        if diff.get("findings_truncated"):
            cut_kinds = diff.get("findings_truncated_kinds") or {}
            cut_total = sum(cut_kinds.values()) if isinstance(cut_kinds, dict) else None
            detail = f" ({cut_total} finding(s) by kind: {cut_kinds})" if cut_kinds else ""
            lines.append(
                "- _(abicheck's own report already truncated `diff.findings` "
                f"before this comment was rendered{detail} -- the full, "
                "untruncated set is not available in this report; re-run with "
                "a higher `--max-findings` for the complete list.)_"
            )
        lines.append("")
    elif counts and any(counts.get(b) for b in ("breaking", "api_break", "risk")):
        # A gating bucket has a non-zero count but `findings` is empty or
        # missing -- an older abicheck version (pre-finding-itemization) or
        # a schema this script hasn't been updated for. Say so explicitly
        # rather than silently showing an empty Findings section under a
        # non-zero count, which reads as "nothing to see" when the report
        # actually says otherwise.
        lines.append(
            "_A gating bucket above is non-zero but this report's `diff` block "
            "carries no itemized `findings` list -- see the full abicheck-report "
            "artifact for the underlying changes._"
        )
        lines.append("")

    additions = diff.get("additions") or []
    quality = diff.get("quality") or []
    if additions or quality:
        lines.append("<details>")
        lines.append(
            f"<summary>Compatible surface changes ({len(additions)} addition(s), "
            f"{len(quality)} other compatible/quality change(s))</summary>"
        )
        lines.append("")
        for entry in additions[:_MAX_RENDERED_FINDINGS]:
            lines.append(_finding_line(entry))
        for entry in quality[:_MAX_RENDERED_FINDINGS]:
            lines.append(_finding_line(entry))
        lines.append("")
        lines.append("</details>")
        lines.append("")

    suppressed = diff.get("suppressed") or []
    suppressed_count = diff.get("suppressed_count") or len(suppressed)
    if suppressed_count:
        lines.append(
            f"ℹ️ **{suppressed_count} finding(s) were suppressed** by an approved "
            "`--suppress` rule and excluded from the buckets above."
        )
        if suppressed:
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>Suppressed findings</summary>")
            lines.append("")
            for entry in suppressed[:_MAX_RENDERED_FINDINGS]:
                pre = entry.get("bucket")
                rule = entry.get("suppression_rule")
                extra = f" (rule: `{rule}`)" if rule else ""
                lines.append(_finding_line(entry) + extra)
            lines.append("")
            lines.append("</details>")
        lines.append("")

    if not findings and not additions and not quality and not suppressed_count and not counts:
        lines.append("_No `diff` findings data in this report (no baseline compare ran)._")
        lines.append("")

    return lines


def _render_bazel_evidence_summary(summary):
    """Render `build_bazel_evidence_pack.py --summary-output`'s counts.

    Distinguishes the requested root target(s) from the full transitive
    closure `cquery deps(...)` resolves -- without this, a report like
    `bazel_targets=32` for a workspace scanning one two-file library reads
    as if 32 *products* were scanned, when it's really 1 root target plus
    31 dependency-closure nodes cquery had to walk to reach it.
    """
    if not summary:
        return []
    lines = ["---", "", "### Bazel target resolution", ""]
    requested = summary.get("root_targets_requested") or []
    resolved = summary.get("root_targets_resolved") or []
    unresolved = summary.get("root_targets_unresolved") or []
    lines.append("| Fact | Value |")
    lines.append("|------|-------|")
    lines.append(f"| Requested root target(s) | {', '.join(f'`{t}`' for t in requested) or '_none given_'} |")
    lines.append(f"| Resolved root target(s) | {', '.join(f'`{t}`' for t in resolved) or '_none_'} |")
    lines.append(f"| Transitive targets (dependency closure) | {summary.get('transitive_target_count', '?')} |")
    lines.append(f"| Compile units | {summary.get('compile_unit_count', '?')} |")
    lines.append(f"| Link units | {summary.get('link_unit_count', '?')} |")
    lines.append("")
    if unresolved:
        lines.append(
            f"> ⚠️ {len(unresolved)} requested root target(s) did not resolve in "
            f"`cquery`'s output: {', '.join(f'`{t}`' for t in unresolved)}."
        )
        lines.append("")
    return lines


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


def render(
    report,
    *,
    base_sha,
    head_sha,
    requested_depth,
    run_url,
    artifact_note,
    coverage_contract=None,
    bazel_evidence_summary=None,
):
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

    lines.extend(_render_findings(report.get("diff")))
    lines.extend(_render_bazel_evidence_summary(bazel_evidence_summary))
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
    parser.add_argument(
        "--bazel-evidence-summary", default="",
        help="Path to scripts/build_bazel_evidence_pack.py's --summary-output JSON (optional)",
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

    bazel_evidence_summary = None
    if args.bazel_evidence_summary:
        try:
            with open(args.bazel_evidence_summary, "r", encoding="utf-8") as fh:
                bazel_evidence_summary = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            # Best-effort only, unlike --coverage-contract: this section is
            # purely informational (never gates), so a missing/unreadable
            # summary (e.g. the Bazel evidence-pack step didn't run) just
            # means the section is omitted, same as never passing the flag.
            bazel_evidence_summary = None

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
            bazel_evidence_summary=bazel_evidence_summary,
        )

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(body)


if __name__ == "__main__":
    sys.exit(main())
