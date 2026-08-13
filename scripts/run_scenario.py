#!/usr/bin/env python3
"""Run scenarios/manifest.yaml's fixture pairs through `abicheck compare`
and check the actual verdict against each scenario's `expected_verdict`
oracle.

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


def run_abicheck_compare(old_lib, new_lib, new_header, output_json, old_header=None):
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
    # A default-argument change is invisible at that depth: the mangled
    # symbol is identical old vs new (Itanium mangling doesn't encode
    # default values), so without the OLD side's own header AST to diff
    # against, abicheck has no old-side default-argument fact to compare
    # the new one to and reports NO_CHANGE regardless of what actually
    # changed (confirmed: this is exactly what happened before old_header
    # was threaded through here -- see the default_argument_added scenario).
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


def run_one(scenario, results_dir):
    name = scenario["name"]
    run_bazel_build(scenario["old_target"], scenario["new_target"])

    output_json = results_dir / f"{name}.json"
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
    run_abicheck_compare(
        REPO_ROOT / scenario["old_output"],
        REPO_ROOT / scenario["new_output"],
        REPO_ROOT / scenario["new_header"],
        output_json,
        old_header=(REPO_ROOT / old_header) if old_header else None,
    )

    try:
        with open(output_json, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        actual = report.get("verdict")
        read_error = None
    except (OSError, json.JSONDecodeError) as exc:
        # abicheck failed to produce a readable report at all -- fail
        # closed (no verdict is never treated as a match), not silently.
        actual = None
        read_error = str(exc)

    expected = scenario["expected_verdict"]
    return {
        "name": name,
        "description": scenario.get("description", "").strip(),
        "expected_verdict": expected,
        "actual_verdict": actual,
        "passed": actual == expected,
        "read_error": read_error,
    }


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
        result = run_one(scenario, results_dir)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"{status}: expected={result['expected_verdict']} "
            f"actual={result['actual_verdict']}"
            + (f" (report unreadable: {result['read_error']})" if result["read_error"] else "")
        )

    summary_path = results_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    failed = [r for r in results if not r["passed"]]
    if failed:
        print(f"\n{len(failed)}/{len(results)} scenario(s) FAILED:", file=sys.stderr)
        for r in failed:
            print(
                f"  - {r['name']}: expected {r['expected_verdict']}, got {r['actual_verdict']}",
                file=sys.stderr,
            )
        return 1

    print(f"\nAll {len(results)} scenario(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
