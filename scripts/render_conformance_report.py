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

Matching is by the flat ``(kind, symbol)`` tuple -- not (yet) abicheck's
own backend-independent ``canonical_finding_id`` (schema 2.35, not present
in the pinned v0.5.0 release this workflow currently runs), which would be
a strictly better key once this lab upgrades its pinned abicheck version.
``old_value``/``new_value`` are compared only as an *annotation* on an
already-kind+symbol-matched pair (flagged as a "value mismatch" rather than
treated as a separate matching dimension), since the two producers are not
guaranteed to spell an old/new type identically (CastXML's ``char const*``
vs. Clang's ``char const *``) -- the same backend-spelling variance
abicheck's own ``finding_identity.canonicalize_type_name`` exists to paper
over report-side, which this lab-side script does not attempt to reproduce.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MARKER_TEMPLATE = "<!-- abicheck-lab-conformance-report:{key} -->"


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


def _report_verdict(report: dict | None) -> str:
    if report is None:
        return "unavailable"
    verdict = report.get("verdict")
    return str(verdict) if verdict is not None else "unknown"


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
    verdict_line = (
        "✅ Verdicts agree"
        if left_verdict == right_verdict
        else "⚠️ **Verdicts disagree**"
    )
    lines.append(f"{verdict_line}: `{left_verdict}` vs. `{right_verdict}`.")
    lines.append("")

    left_findings = _extract_findings(left)
    right_findings = _extract_findings(right)

    def _key(f: dict[str, str]) -> tuple[str, str]:
        return (f["kind"], f["symbol"])

    left_by_key = {_key(f): f for f in left_findings}
    right_by_key = {_key(f): f for f in right_findings}
    left_keys = set(left_by_key)
    right_keys = set(right_by_key)

    matched = left_keys & right_keys
    only_left = left_keys - right_keys
    only_right = right_keys - left_keys

    value_mismatches = [
        k
        for k in matched
        if (left_by_key[k]["old_value"], left_by_key[k]["new_value"])
        != (right_by_key[k]["old_value"], right_by_key[k]["new_value"])
    ]

    lines.append(
        f"- {len(matched)} finding(s) matched by (kind, symbol) in both reports "
        f"({len(value_mismatches)} with differing old/new value text -- may be a "
        "genuine cross-backend type-spelling difference, not necessarily a bug)."
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

    def _render_section(title: str, keys: set[tuple[str, str]], cap: int = 20) -> None:
        if not keys:
            return
        lines.append(f"<details><summary>{title} ({len(keys)})</summary>")
        lines.append("")
        for kind, symbol in sorted(keys)[:cap]:
            lines.append(f"- `{kind}`: `{symbol}`")
        if len(keys) > cap:
            lines.append(f"- _(and {len(keys) - cap} more)_")
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
        for kind, symbol in sorted(value_mismatches)[:20]:
            lf = left_by_key[(kind, symbol)]
            rf = right_by_key[(kind, symbol)]
            lines.append(
                f"- `{kind}`: `{symbol}` -- {left_label}: "
                f"`{lf['old_value']}` → `{lf['new_value']}`; {right_label}: "
                f"`{rf['old_value']}` → `{rf['new_value']}`"
            )
        if len(value_mismatches) > 20:
            lines.append(f"- _(and {len(value_mismatches) - 20} more)_")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    if not only_left and not only_right:
        lines.append("✅ No producer-only findings -- the two producers fully agree.")

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
