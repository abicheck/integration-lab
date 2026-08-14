#!/usr/bin/env python3
"""Render a producer-conformance report comparing two ABICheck JSON reports
that analyze the *same* library/header pair through two different
producers -- either two header-AST frontends (CastXML vs. Clang, the L2
cell of the review's evidence-tier matrix) or two L4 source-fact producers
(the portable replay path vs. the zero-extra-parse Clang plugin).

Why this exists: this lab's `abi-scan.yml` already runs all four legs of
that matrix (`l2-castxml`, `l2-clang`, `l4-clang-replay`, `l4-clang-plugin`)
on every ABI-relevant PR and uploads each as its own artifact, but nothing
ever compared them to each other -- a producer disagreeing with its sibling
(a real divergence, or a bug in one of the two) was only discoverable by a
human manually diffing two JSON artifacts by hand. This script is that
comparison, made repeatable and machine-readable.

Both `mode: compare` (l2-castxml, l2-clang, l4-clang-plugin -- a flat
top-level `changes` list) and `mode: scan` (l4-clang-replay -- findings
under `diff.findings`/`diff.additions`/`diff.quality`) report shapes are
supported transparently (`_extract_findings` below): the two producer
pairs this script is actually run against straddle both mode shapes
(`l4-clang-replay` is `scan`, `l4-clang-plugin` is `compare`), so a
single-shape parser would silently see one side as empty instead of
comparing anything.

Matching is a *multiset* match on ``(kind, symbol)`` (Codex review: an
earlier revision collapsed same-key findings into a dict, silently
dropping a duplicate -- e.g. two ``PARAM_TYPE_CHANGED`` findings on the
same function for two different parameters -- into one). Findings whose
full ``(kind, symbol, old_value, new_value)`` tuple matches exactly are
counted as agreeing; the *remainder* on each side is then paired up by
``(kind, symbol)`` alone to report as a "value mismatch" (old/new differs
on an otherwise-matched finding -- may be a genuine cross-backend
type-spelling difference, e.g. CastXML's ``char const*`` vs. Clang's
``char const *``, not necessarily a bug -- this lab-side script does not
attempt abicheck's own ``finding_identity.canonicalize_type_name``
normalization), with only the true leftover reported as producer-only.
Not (yet) using abicheck's own backend-independent ``canonical_finding_id``
(schema 2.35, not present in the pinned v0.5.0 release this workflow
currently runs), which would be a strictly better key once this lab
upgrades its pinned abicheck version.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

MARKER_TEMPLATE = "<!-- abicheck-lab-conformance-report:{key} -->"

#: `compare` reports an unchanged pair as `NO_CHANGE`; `scan` reports the
#: same "nothing gated" outcome as `COMPATIBLE` (see scenarios/manifest.yaml
#: and render_scan_comment.py's own `_VERDICT_LINES`, respectively) -- two
#: spellings of the same "clean" verdict, not a real disagreement (Codex
#: review, fresh evidence: comparing raw strings falsely flagged the L4
#: replay/plugin pair as disagreeing on every implementation-only change).
_EQUIVALENT_CLEAN_VERDICTS = frozenset({"NO_CHANGE", "COMPATIBLE"})


def _load_report(path: Path | None) -> dict | None:
    """Return the parsed JSON report at *path*, or None if unavailable.

    A missing/empty file is a normal, expected outcome here -- the
    `l4_clang_plugin` job this script's l4-clang-replay/l4-clang-plugin
    pairing depends on is documented as best-effort and skips gracefully
    (see abi-scan.yml's own "Report l4-clang-plugin profile status" step),
    so "one side never produced a report" must degrade to a clearly labeled
    section of the output, not a script failure.
    """
    if path is None:
        return None
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _extract_findings(report: dict | None) -> list[dict[str, str]]:
    """Return a flat list of ``{kind, symbol, old_value, new_value}`` dicts
    from *report*, regardless of whether it's a `compare`-mode report (a
    top-level ``changes`` list) or a `scan`-mode one (``diff.findings`` /
    ``diff.additions`` / ``diff.quality``). See module docstring for why
    both shapes matter here.
    """
    if report is None:
        return []
    out: list[dict[str, str]] = []
    if isinstance(report.get("changes"), list):
        for c in report["changes"]:
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "kind": _stringify(c.get("kind")),
                    "symbol": _stringify(c.get("symbol")),
                    "old_value": _stringify(c.get("old_value")),
                    "new_value": _stringify(c.get("new_value")),
                }
            )
        return out
    diff = report.get("diff")
    if isinstance(diff, dict):
        for section in ("findings", "additions", "quality"):
            entries = diff.get(section)
            if not isinstance(entries, list):
                continue
            for c in entries:
                if not isinstance(c, dict):
                    continue
                out.append(
                    {
                        "kind": _stringify(c.get("kind")),
                        "symbol": _stringify(c.get("symbol")),
                        "old_value": _stringify(c.get("old_value")),
                        "new_value": _stringify(c.get("new_value")),
                    }
                )
    return out


def _findings_truncated(report: dict | None) -> tuple[bool, dict]:
    """Whether *report*'s own ``diff.findings`` was already truncated by
    abicheck itself before this script ever saw it (``scan``-mode reports
    only -- see ``scripts/render_scan_comment.py``'s identical check).

    A `compare`-mode report's flat ``changes`` list has no equivalent
    truncation flag in this codebase, so this only ever fires for the
    `scan`-mode side of a pair (Codex review: without this, every finding
    upstream already cut from a truncated `l4-clang-replay` scan read as a
    false "plugin-only" producer divergence instead of "unknown -- replay's
    own report was incomplete").
    """
    if report is None:
        return False, {}
    diff = report.get("diff")
    if not isinstance(diff, dict):
        return False, {}
    truncated = bool(diff.get("findings_truncated"))
    cut_kinds = diff.get("findings_truncated_kinds")
    return truncated, cut_kinds if isinstance(cut_kinds, dict) else {}


def _report_verdict(report: dict | None) -> str:
    if report is None:
        return "unavailable"
    verdict = report.get("verdict")
    return str(verdict) if verdict is not None else "unknown"


def _normalize_verdict(verdict: str) -> str:
    return "CLEAN" if verdict in _EQUIVALENT_CLEAN_VERDICTS else verdict


def _multiset(findings: list[dict[str, str]]) -> Counter:
    return Counter(
        (f["kind"], f["symbol"], f["old_value"], f["new_value"]) for f in findings
    )


def _group_by_kind_symbol(
    counter: Counter,
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Expand a ``(kind, symbol, old, new)`` multiset (with its counts)
    into ``{(kind, symbol): [(old, new), ...]}`` -- one list entry per
    occurrence, preserving duplicates."""
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for (kind, symbol, old, new), count in counter.items():
        grouped[(kind, symbol)].extend([(old, new)] * count)
    return grouped


def build_report(
    *,
    left_label: str,
    left: dict | None,
    right_label: str,
    right: dict | None,
    report_key: str,
) -> str:
    """Return the rendered Markdown conformance report body."""
    lines: list[str] = [MARKER_TEMPLATE.format(key=report_key), ""]
    lines.append(f"### Producer conformance: {left_label} vs. {right_label}")
    lines.append("")

    if left is None or right is None:
        missing = left_label if left is None else right_label
        lines.append(
            f"⏭️ **Skipped** -- no report available for **{missing}** this run "
            "(a best-effort producer job did not run or produced nothing; "
            "see its own status summary above)."
        )
        return "\n".join(lines) + "\n"

    left_verdict = _report_verdict(left)
    right_verdict = _report_verdict(right)
    if _normalize_verdict(left_verdict) == _normalize_verdict(right_verdict):
        verdict_line = f"✅ Verdicts agree: `{left_verdict}` vs. `{right_verdict}`."
    else:
        verdict_line = f"⚠️ **Verdicts disagree**: `{left_verdict}` vs. `{right_verdict}`."
    lines.append(verdict_line)
    lines.append("")

    left_truncated, left_cut_kinds = _findings_truncated(left)
    right_truncated, right_cut_kinds = _findings_truncated(right)
    if left_truncated or right_truncated:
        lines.append(
            "⚠️ **At least one side's own report already truncated its findings "
            "list before this comparison ran** -- any producer-only finding "
            "below may just be a truncated-away entry, not a real divergence:"
        )
        if left_truncated:
            lines.append(f"  - {left_label}: truncated ({left_cut_kinds or 'unknown counts'}).")
        if right_truncated:
            lines.append(f"  - {right_label}: truncated ({right_cut_kinds or 'unknown counts'}).")
        lines.append("")

    left_findings = _extract_findings(left)
    right_findings = _extract_findings(right)

    left_ms = _multiset(left_findings)
    right_ms = _multiset(right_findings)
    exact_matched = left_ms & right_ms
    matched_count = sum(exact_matched.values())
    left_remainder = left_ms - exact_matched
    right_remainder = right_ms - exact_matched

    left_grouped = _group_by_kind_symbol(left_remainder)
    right_grouped = _group_by_kind_symbol(right_remainder)

    value_mismatches: list[tuple[str, str, tuple[str, str], tuple[str, str]]] = []
    only_left: list[tuple[str, str, tuple[str, str]]] = []
    only_right: list[tuple[str, str, tuple[str, str]]] = []

    for key in sorted(set(left_grouped) | set(right_grouped)):
        lvals = left_grouped.get(key, [])
        rvals = right_grouped.get(key, [])
        n = min(len(lvals), len(rvals))
        for i in range(n):
            value_mismatches.append((*key, lvals[i], rvals[i]))
        for v in lvals[n:]:
            only_left.append((*key, v))
        for v in rvals[n:]:
            only_right.append((*key, v))

    lines.append(
        f"- {matched_count} finding(s) match exactly (same kind, symbol, and "
        "old/new value) in both reports."
    )
    lines.append(
        f"- {len(value_mismatches)} finding(s) matched by (kind, symbol) but with "
        "differing old/new value text -- may be a genuine cross-backend "
        "type-spelling difference, not necessarily a bug."
    )
    lines.append(
        f"- {len(only_left)} finding(s) present only in **{left_label}** "
        f"({len(left_findings)} total)."
    )
    lines.append(
        f"- {len(only_right)} finding(s) present only in **{right_label}** "
        f"({len(right_findings)} total)."
    )
    lines.append("")

    def _render_section(
        title: str, entries: list[tuple[str, str, tuple[str, str]]], cap: int = 20
    ) -> None:
        if not entries:
            return
        lines.append(f"<details><summary>{title} ({len(entries)})</summary>")
        lines.append("")
        for kind, symbol, (old, new) in entries[:cap]:
            lines.append(f"- `{kind}`: `{symbol}` (`{old}` → `{new}`)")
        if len(entries) > cap:
            lines.append(f"- _(and {len(entries) - cap} more)_")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    _render_section(f"Only in {left_label}", only_left)
    _render_section(f"Only in {right_label}", only_right)
    if value_mismatches:
        lines.append(
            f"<details><summary>Value mismatches on matched findings ({len(value_mismatches)})</summary>"
        )
        lines.append("")
        for kind, symbol, (lold, lnew), (rold, rnew) in value_mismatches[:20]:
            lines.append(
                f"- `{kind}`: `{symbol}` -- {left_label}: "
                f"`{lold}` → `{lnew}`; {right_label}: `{rold}` → `{rnew}`"
            )
        if len(value_mismatches) > 20:
            lines.append(f"- _(and {len(value_mismatches) - 20} more)_")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # "Fully agree" additionally requires: (a) neither side's own findings
    # list was truncated -- an omitted, truncated-away finding makes
    # agreement unknowable, not confirmed (Codex review, fresh evidence:
    # the truncation warning above was rendered but not factored into this
    # verdict, so a truncated scan with a matching visible subset still
    # printed a green "fully agree"); and (b) the two sides' normalized
    # verdicts actually agree -- an identical finding multiset can still
    # carry two different overall verdicts (e.g. one report's policy
    # resolves the same findings to BREAKING, the other to API_BREAK), and
    # printing "fully agree" right after an already-rendered "Verdicts
    # disagree" line above is self-contradictory (Codex review, fresh
    # evidence).
    findings_truncated = left_truncated or right_truncated
    verdicts_agree = _normalize_verdict(left_verdict) == _normalize_verdict(right_verdict)
    if not only_left and not only_right and not value_mismatches:
        if findings_truncated:
            lines.append(
                "ℹ️ The visible findings fully agree, but at least one side's "
                "report was truncated (see warning above) -- full agreement "
                "cannot be confirmed."
            )
        elif not verdicts_agree:
            lines.append(
                "⚠️ The two producers agree on every individual finding, but "
                "their overall **verdicts disagree** (see above) -- this is "
                "not full agreement."
            )
        else:
            lines.append("✅ No producer-only findings and no value mismatches -- the two producers fully agree.")
    elif not only_left and not only_right:
        lines.append(
            "ℹ️ The two producers agree on **which** findings exist, but "
            f"differ in reported old/new values for {len(value_mismatches)} "
            "of them (see above)."
        )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True, help="Left report JSON path")
    parser.add_argument("--left-label", required=True)
    parser.add_argument("--right", type=Path, required=True, help="Right report JSON path")
    parser.add_argument("--right-label", required=True)
    parser.add_argument(
        "--report-key",
        required=True,
        help="Short slug identifying this comparison (used in the HTML marker comment).",
    )
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)

    left = _load_report(args.left)
    right = _load_report(args.right)

    body = build_report(
        left_label=args.left_label,
        left=left,
        right_label=args.right_label,
        right=right,
        report_key=args.report_key,
    )
    args.output.write_text(body, encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
