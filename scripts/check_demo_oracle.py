#!/usr/bin/env python3
"""The scenario oracle for a generated demonstration PR (roadmap item 14).

Each `test/*` branch in this repository exists to demonstrate exactly one
ABI outcome against the real gate. The gate itself reports whatever it finds
-- that is the *natural* result, and for two of these branches it is
deliberately red. A red gate on a branch whose whole purpose is to be red
carries no information on its own: it is equally consistent with "the demo
worked" and with "the demo drifted and now breaks for some other reason".

This is the separate check that tells those apart. It reads the report the
ordinary gate already produced -- not a second abicheck invocation, which
could drift from the gate and quietly assert something the gate never ran --
and is green only when that natural result is the declared one.

What is asserted, and why in that shape, is documented in
`demos/manifest.yaml`: the verdict exactly, plus required and forbidden
findings rather than exact set equality.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

#: Verdicts on which the gate is expected to fail the PR.
RED_VERDICTS = frozenset({"API_BREAK", "BREAKING"})


class OracleError(RuntimeError):
    """The oracle could not be evaluated at all (bad manifest, bad report)."""


def load_demo(manifest: Path, demo_id: str) -> dict[str, Any]:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    for entry in doc.get("demonstrations") or []:
        if entry.get("id") == demo_id:
            return entry
    known = ", ".join(sorted(e.get("id", "?") for e in doc.get("demonstrations") or []))
    raise OracleError(f"no demonstration {demo_id!r} in {manifest}; known ids: {known}")


def demo_for_branch(manifest: Path, branch: str) -> dict[str, Any] | None:
    """The demonstration a head branch belongs to, or None.

    Returning None is a legitimate answer -- the oracle job runs on every PR
    and most PRs are not demonstrations -- but it is the CALLER's job to
    distinguish that from a failure, which is why this never raises.
    """
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    for entry in doc.get("demonstrations") or []:
        if entry.get("branch") == branch:
            return entry
    return None


def findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    changes = report.get("changes")
    if not isinstance(changes, list):
        raise OracleError("report has no `changes` list; it is not a compare report")
    return [c for c in changes if isinstance(c, dict)]


def _matches(finding: dict[str, Any], want: dict[str, Any]) -> bool:
    """Whether *finding* satisfies a manifest matcher.

    A matcher naming only a `kind` matches any finding of that kind -- that
    is what makes `forbidden_findings: [{kind: func_removed}]` mean "no
    binary removal of any symbol", which is the assertion the source-only
    break needs. Naming a `symbol` too narrows it to that one.
    """
    for key, value in want.items():
        if str(finding.get(key)) != str(value):
            return False
    return True


def evaluate(demo: dict[str, Any], report: dict[str, Any]) -> list[str]:
    expect = demo.get("expect") or {}
    errors: list[str] = []

    verdict = report.get("verdict")
    if verdict != expect.get("verdict"):
        errors.append(f"verdict={verdict!r}, expected {expect.get('verdict')!r}")

    # The gate colour is derived from the verdict rather than declared
    # independently, so a manifest cannot claim "green" for a BREAKING demo.
    expected_gate = expect.get("gate")
    actual_gate = "red" if verdict in RED_VERDICTS else "green"
    if expected_gate is not None and actual_gate != expected_gate:
        errors.append(
            f"gate={actual_gate} (from verdict {verdict!r}), expected {expected_gate}"
        )

    present = findings(report)
    for want in expect.get("required_findings") or []:
        if not any(_matches(f, want) for f in present):
            errors.append(f"missing required finding {want!r}")
    for unwanted in expect.get("forbidden_findings") or []:
        hits = [f for f in present if _matches(f, unwanted)]
        if hits:
            errors.append(
                f"forbidden finding {unwanted!r} present: "
                + ", ".join(sorted(str(f.get("symbol")) for f in hits))
            )

    operational = report.get("operational_errors")
    if operational:
        # A demonstration whose report carries operational errors is not
        # demonstrating a verdict, it is demonstrating a broken run -- even
        # when the verdict happens to land on the expected value.
        errors.append(f"report carries {len(operational)} operational error(s): {operational!r}")
    return errors


def render(demo: dict[str, Any], errors: list[str], report: dict[str, Any]) -> str:
    lines = [
        f"### Scenario oracle — `{demo['id']}`",
        "",
        demo.get("claim", ""),
        "",
        f"Natural gate verdict: `{report.get('verdict')}`  "
        f"(expected `{(demo.get('expect') or {}).get('verdict')}`)",
        "",
    ]
    if errors:
        lines.append("❌ This branch no longer demonstrates what it claims:")
        lines += [f"- {e}" for e in errors]
    else:
        lines.append("✅ The natural result is exactly the declared one.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("demos/manifest.yaml"))
    parser.add_argument("--report", type=Path, required=True,
                        help="the report the ordinary gate produced")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", help="demonstration id")
    group.add_argument("--branch", help="head branch; exits 0 when it is not a demonstration")
    parser.add_argument("--summary", type=Path, help="write a markdown verdict here")
    args = parser.parse_args(argv)

    try:
        if args.demo:
            demo = load_demo(args.manifest, args.demo)
        else:
            demo = demo_for_branch(args.manifest, args.branch)
            if demo is None:
                print(f"{args.branch} is not a demonstration branch; nothing to assert")
                return 0
        if not args.report.is_file() or not args.report.stat().st_size:
            raise OracleError(
                f"the gate produced no report at {args.report}; the oracle cannot "
                "confirm a natural result that does not exist"
            )
        report = json.loads(args.report.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise OracleError(f"{args.report} is not a JSON object")
        errors = evaluate(demo, report)
    except (OracleError, json.JSONDecodeError) as exc:
        print(f"demo-oracle: {exc}", file=sys.stderr)
        return 2

    text = render(demo, errors, report)
    print(text)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
    for error in errors:
        print(f"FAIL {demo['id']}: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
