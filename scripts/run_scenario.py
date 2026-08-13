#!/usr/bin/env python3
"""Run scenarios/manifest.yaml's fixture pairs through `abicheck compare`
and check the actual verdict against each scenario's `expected_verdict`
(or per-profile `expected`) oracle.

Why this exists: the gate this repo runs on itself (abi-scan.yml) only
ever exercises whatever the *current* PR's diff to //:math happens to be
-- it never actually proves abicheck detects a real binary/API break, or
correctly leaves a compatible change alone. This is the "validation
platform" layer: independent fixture pairs, each with a machine-readable
expected outcome, run and checked automatically in CI.

Each scenario builds two tiny, independent Bazel targets (fixtures/<name>/
v1 and v2, listed in scenarios/manifest.yaml) and runs `abicheck compare`
between them -- binary + header depth only, deliberately not `depth:
source` (that source-evidence pipeline is what abi-scan.yml's own `scan`
job already exercises against the real //:math library; this layer's job
is verdict-detection correctness on deliberately small, easy-to-reason-
about fixtures, not re-proving the source pipeline).

Assumes `bazel` and the `abicheck` CLI are already on PATH (the CI
workflow installs both before calling this script) -- this script itself
never installs anything, so it stays runnable the same way locally.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "run_scenario: PyYAML is required (pip install pyyaml)",
        file=sys.stderr,
    )
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("scenarios", [])


def run_bazel_build(*targets):
    subprocess.run(["bazel", "build", *targets], check=True)


def run_abicheck_compare(
    old_lib, new_lib, new_header, output_json, old_header=None, ast_frontend=None, suppress=None
):
    # abicheck's bare CLI (unlike the GitHub Action wrapper) exits
    # non-zero on a gated result by default -- that's exactly what a
    # BREAKING scenario is supposed to produce, so this deliberately does
    # NOT check=True. The verdict field in the written report, not the
    # process exit code, is what this script actually judges.
    #
    # old_header is optional and defaults to None (old side stays
    # DWARF/symbols-only, as every scenario before default_argument_added
    # relies on -- a mangled-symbol-visible change like a removed/added
    # function or a changed parameter type needs no header on either side).
    # A default-argument fact needs both sides' header ASTs to even be
    # comparable at all (without the OLD side's own header, abicheck has no
    # old-side default-argument fact to compare the new one to) -- passed
    # here so the comparison is real rather than trivially vacuous, even
    # though the actual, verified abicheck behavior for *gaining* a default
    # is COMPATIBLE either way (see scenarios/manifest.yaml's
    # default_argument_added entry for the full citation).
    cmd = [
        "abicheck",
        "compare",
        str(old_lib),
        str(new_lib),
        "--header",
        f"new={new_header}",
    ]
    if old_header is not None:
        cmd += ["--header", f"old={old_header}"]
    # ast_frontend is optional and defaults to None (abicheck's own
    # default, currently CastXML -- AGENTS.md: "dumper_castxml.py ...
    # default L2 header backend"). Passed explicitly when a scenario's
    # manifest entry declares per-profile expectations (`expected:`), so
    # the same fixture pair can be run once per header frontend and
    # checked against each frontend's own oracle verdict.
    if ast_frontend is not None:
        cmd += ["--ast-frontend", ast_frontend]
    # suppress is optional: a scenario declaring `suppress:` (repo-relative
    # path to a YAML rule file) proves a scenario's own gating finding
    # stops gating once suppressed, while staying visible in the report's
    # own suppression audit trail (report["suppression"]["suppressed_count"]/
    # ["suppressed_changes"]) rather than the underlying change simply
    # never being found. Verified locally against a real remove_function
    # pair: unsuppressed verdict BREAKING, suppressed verdict NO_CHANGE
    # with suppressed_count: 1 -- see scenarios/manifest.yaml's
    # remove_function_suppressed entry.
    if suppress is not None:
        cmd += ["--suppress", str(suppress)]
    cmd += [
        "--version",
        "old=old",
        "--version",
        "new=new",
        "--lang",
        "c++",
        "--format",
        "json",
        "-o",
        str(output_json),
        "--policy",
        "strict_abi",
    ]
    subprocess.run(cmd, check=False)


def _scenario_profiles(scenario):
    """Return {profile_name: expected_verdict} for *scenario*.

    A scenario declares EITHER a single, backward-compatible
    `expected_verdict` (run once, with abicheck's own default --ast-frontend
    -- every scenario before this function existed uses this form) OR a
    per-profile `expected: {castxml: V, clang: V, ...}` map (run once per
    named profile, each with `--ast-frontend <profile>` explicit). The two
    are mutually exclusive in one manifest entry -- a scenario whose
    verdict genuinely differs by header frontend (or that specifically
    wants to pin agreement across frontends) uses `expected`; everything
    else keeps the simpler single-oracle form.
    """
    expected_map = scenario.get("expected")
    if expected_map:
        return dict(expected_map)
    return {None: scenario["expected_verdict"]}


def run_one_profile(scenario, profile, expected, results_dir):
    name = scenario["name"]
    result_name = name if profile is None else f"{name}.{profile}"
    output_json = results_dir / f"{result_name}.json"
    # run_abicheck_compare() deliberately doesn't check the subprocess
    # return code -- a BREAKING scenario is *supposed* to exit non-zero.
    # But that also means a run that fails before writing anything (a
    # crash, a bad flag) would otherwise leave whatever report an earlier
    # invocation left at this same path (a local re-run reusing
    # --results-dir, or a retried CI step) sitting there looking fresh,
    # and the read below would happily report its stale verdict as if
    # this run had produced it (Codex review). Remove any pre-existing
    # report first so "no report" always means exactly that.
    output_json.unlink(missing_ok=True)
    old_header = scenario.get("old_header")
    suppress = scenario.get("suppress")
    run_abicheck_compare(
        REPO_ROOT / scenario["old_output"],
        REPO_ROOT / scenario["new_output"],
        REPO_ROOT / scenario["new_header"],
        output_json,
        old_header=(REPO_ROOT / old_header) if old_header else None,
        ast_frontend=profile,
        suppress=(REPO_ROOT / suppress) if suppress else None,
    )

    expected_suppressed_count = scenario.get("expected_suppressed_count")
    # Codex review: checking suppressed_count alone (e.g. == 1) doesn't
    # prove WHICH finding was suppressed -- suppression_partial's own
    # regression is exactly the case this misses: if the suppression rule
    # accidentally matched required_api's change instead of legacy_metric's
    # (leaving legacy_metric's removal unsuppressed), the verdict would
    # still read BREAKING and suppressed_count would still read 1 -- a
    # false pass on the scenario this repo's review calls "the central
    # case". expected_suppressed_symbols (a list of mangled symbols, when
    # given) checks the actual identity of every suppressed_changes entry,
    # not just how many there were.
    expected_suppressed_symbols = scenario.get("expected_suppressed_symbols")
    # Companion to expected_suppressed_symbols: which symbol(s) are still
    # gating (report["changes"], the non-suppressed findings) -- pins that
    # the ONE real, unaccepted break is still present and unsuppressed,
    # closing the same false-pass gap from the other direction.
    expected_gating_symbols = scenario.get("expected_gating_symbols")
    try:
        with open(output_json, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        actual = report.get("verdict")
        suppression_block = report.get("suppression") or {}
        actual_suppressed_count = suppression_block.get("suppressed_count")
        actual_suppressed_symbols = sorted(
            c.get("symbol") for c in (suppression_block.get("suppressed_changes") or [])
        )
        actual_gating_symbols = sorted(
            c.get("symbol") for c in (report.get("changes") or [])
        )
        read_error = None
    except (OSError, json.JSONDecodeError) as exc:
        # abicheck failed to produce a readable report at all -- fail
        # closed (no verdict is never treated as a match), not silently.
        actual = None
        actual_suppressed_count = None
        actual_suppressed_symbols = None
        actual_gating_symbols = None
        read_error = str(exc)

    verdict_passed = actual == expected
    # Only checked when a scenario opts in (expected_suppressed_count set) --
    # this is what turns the suppression scenario's assertion from "the
    # verdict happens to read NO_CHANGE" into "verdict is NO_CHANGE *because*
    # exactly one finding was suppressed", the distinction the review's own
    # suppression-scenario design calls out (a suppressed finding must stay
    # visible in the audit trail, not just silently vanish).
    suppressed_count_passed = (
        expected_suppressed_count is None or actual_suppressed_count == expected_suppressed_count
    )
    suppressed_symbols_passed = (
        expected_suppressed_symbols is None
        or actual_suppressed_symbols == sorted(expected_suppressed_symbols)
    )
    gating_symbols_passed = (
        expected_gating_symbols is None
        or actual_gating_symbols == sorted(expected_gating_symbols)
    )
    return {
        "name": name,
        "profile": profile,
        "description": scenario.get("description", "").strip(),
        "expected_verdict": expected,
        "actual_verdict": actual,
        "expected_suppressed_count": expected_suppressed_count,
        "actual_suppressed_count": actual_suppressed_count,
        "expected_suppressed_symbols": expected_suppressed_symbols,
        "actual_suppressed_symbols": actual_suppressed_symbols,
        "expected_gating_symbols": expected_gating_symbols,
        "actual_gating_symbols": actual_gating_symbols,
        "passed": (
            verdict_passed
            and suppressed_count_passed
            and suppressed_symbols_passed
            and gating_symbols_passed
        ),
        "read_error": read_error,
    }


def run_one(scenario, results_dir):
    """Build *scenario*'s fixture pair once, then run every declared profile
    against it. Returns a list of per-profile results (length 1 for a
    single-oracle scenario, one per named profile for a per-profile one)."""
    run_bazel_build(scenario["old_target"], scenario["new_target"])
    profiles = _scenario_profiles(scenario)
    return [
        run_one_profile(scenario, profile, expected, results_dir)
        for profile, expected in profiles.items()
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="scenarios/manifest.yaml",
        help="Path (repo-relative) to the scenario manifest",
    )
    parser.add_argument(
        "--results-dir",
        default="scenario-results",
        help="Directory (repo-relative) to write per-scenario reports and a summary to",
    )
    parser.add_argument(
        "--only",
        help="Run only the named scenario (default: run every scenario in the manifest)",
    )
    args = parser.parse_args()

    manifest_path = REPO_ROOT / args.manifest
    scenarios = load_manifest(manifest_path)
    if not scenarios:
        print(f"run_scenario: no scenarios found in {manifest_path}", file=sys.stderr)
        return 1

    if args.only:
        scenarios = [s for s in scenarios if s["name"] == args.only]
        if not scenarios:
            print(
                f"run_scenario: no scenario named {args.only!r} in {manifest_path}",
                file=sys.stderr,
            )
            return 1

    results_dir = REPO_ROOT / args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for scenario in scenarios:
        print(f"--- {scenario['name']} ---")
        # run_one() now runs every profile a scenario declares against one
        # built fixture pair and returns one result dict per profile --
        # length 1 for the ordinary single-oracle scenarios, one per named
        # profile for a per-profile `expected:` scenario.
        for result in run_one(scenario, results_dir):
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            profile_label = f" [{result['profile']}]" if result["profile"] else ""
            suppressed_label = (
                f" suppressed_count(expected={result['expected_suppressed_count']}, "
                f"actual={result['actual_suppressed_count']})"
                if result["expected_suppressed_count"] is not None
                else ""
            )
            suppressed_symbols_label = (
                f" suppressed_symbols(expected={result['expected_suppressed_symbols']}, "
                f"actual={result['actual_suppressed_symbols']})"
                if result["expected_suppressed_symbols"] is not None
                else ""
            )
            print(
                f"{status}{profile_label}: expected={result['expected_verdict']} "
                f"actual={result['actual_verdict']}{suppressed_label}{suppressed_symbols_label}"
                + (f" (report unreadable: {result['read_error']})" if result["read_error"] else "")
            )

    summary_path = results_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    failed = [r for r in results if not r["passed"]]
    if failed:
        print(f"\n{len(failed)}/{len(results)} scenario/profile run(s) FAILED:", file=sys.stderr)
        for r in failed:
            profile_label = f" [{r['profile']}]" if r["profile"] else ""
            print(
                f"  - {r['name']}{profile_label}: expected {r['expected_verdict']}, got {r['actual_verdict']}",
                file=sys.stderr,
            )
        return 1

    print(f"\nAll {len(results)} scenario/profile run(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
