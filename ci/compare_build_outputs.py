#!/usr/bin/env python3
"""PR3 item 1: pairwise cross-build-system equivalence check.

Compares every pair of already-staged `abicheck-build-<profile-id>/`
directories (the same canonical shape `ci/emit_build_output.py` produces,
built from the SAME git commit -- this script does not itself build
anything or check that the commits match; the caller is responsible for
running `ci/run_profile.py` once per profile first, same as
`ci/check_profile.py`/`ci/check_profile_coverage.py` already assume) and
answers one question per shared target: does Bazel's `libmath.so` mean the
same thing, ABI-wise, as CMake's and Make's?

This is explicitly NOT a byte-for-byte snapshot comparison (design doc
section 19's own "do not compare directly" list) -- a build-system target
id, an evidence producer path, a compile database location, or a
`generated_at` timestamp legitimately differs between two profiles built
from identical source and must never be reported as a finding. What DOES
get compared, and reported as a real finding when it differs:

  * exported dynamic symbol set (name/type/size, via `nm -D`, reusing
    `ci/check_profile.py`'s own `dynamic_symbols()` -- the same mechanism
    that script's own per-profile baseline check already trusts)
  * SONAME (`readelf -d`, reusing `ci/check_profile.py`'s `_soname()`)
  * dynamic dependency (`NEEDED`) entries (`readelf -d`, new here --
    check_profile.py has no equivalent since a single profile's own
    baseline has nothing to compare its own NEEDED list against)
  * public header inventory: file set AND per-file content digest, reusing
    `ci/check_profile.py`'s `_public_headers_for_target()` -- these are
    literal copies of the exact same repo file on every profile, so a
    content-digest mismatch here is never "expected build-system noise",
    it is a staging bug and reported as such
  * compiler family/C++ standard (`build-output.json`'s own `compiler`
    block) -- expected to match across every profile in `ci/profiles.yaml`
    today (all pinned gcc-14/c++17); a mismatch would mean the profiles
    being compared aren't actually pinned to the same toolchain and is
    flagged as a real toolchain/configuration difference, not silently
    accepted as "well, it's a different build system"

Every difference this script finds is written with one of five
classifications (design doc section 19's own taxonomy) so a human (or the
`integration_gate` job) can tell a real cross-build regression apart from
expected build-system bookkeeping:

  public_contract_mismatch           -- a real ABI-relevant difference
  toolchain_configuration_mismatch   -- same toolchain family/standard
                                         intent, different link/compiler
                                         behavior (e.g. differing NEEDED
                                         entries from linker defaults)
  expected_build_system_metadata_difference -- target ids, generator,
                                         build_command, root paths: always
                                         differ, never a finding on their
                                         own, recorded for context only
  evidence_only_difference           -- evidence producer/path differences
                                         (compile_commands.json location,
                                         bazel-query dir, ...)
  unclassified_difference            -- anything this script cannot place
                                         in one of the above (currently:
                                         staged public-header content not
                                         byte-identical, which should be
                                         structurally impossible given both
                                         sides copy the same repo file --
                                         treated as a real finding, not
                                         swept into "expected metadata")

This is advisory-only groundwork (design doc section 19's own "initially
make this job advisory. Promote it to required only after the build
definitions are intentionally aligned and stable"): `cross_build_equivalence`
in .github/workflows/integration-shadow.yml runs this script with
`continue-on-error: true`, same as every other PR2 check step in that
workflow, and this workflow is not wired into branch-protection required
checks.

## The optional `abicheck`-backed deep check

When the real `abicheck` CLI is importable on PATH (the same package
`abi-scan.yml` installs -- see `ci/check_profile.py`'s own module
docstring for why that scanner is otherwise treated as unavailable in this
lab's own sandbox), this script also runs one real `abicheck compare`
between each pair's built library directly (profile A's `.so` as `old`,
profile B's as `new`, using either side's identically-staged public
header) for every shared target. Unlike the nm/readelf diff above, this
exercises abicheck's actual DWARF/source-aware comparison engine -- it can
notice a struct-layout or default-argument difference the symbol-table
diff structurally cannot (see check_profile.py's own docstring on that
limit). A verdict other than NO_CHANGE/COMPATIBLE from this step is
recorded as a `public_contract_mismatch` finding with
`mechanism: abicheck-compare`. When `abicheck` is not on PATH, this step
is recorded as `not_run` per target, never silently treated as
"equivalent" -- a missing check is not a passing check.

Usage:

    python3 ci/compare_build_outputs.py \\
      --build-dir linux-x86_64-gcc14-cxx17-bazel=abicheck-build-linux-x86_64-gcc14-cxx17-bazel \\
      --build-dir linux-x86_64-gcc14-cxx17-cmake-ninja=abicheck-build-linux-x86_64-gcc14-cxx17-cmake-ninja \\
      --build-dir linux-x86_64-gcc14-cxx17-make-bear=abicheck-build-linux-x86_64-gcc14-cxx17-make-bear \\
      --output-json cross-build-equivalence.json \\
      --output-md cross-build-equivalence.md

At least two `--build-dir` entries are required (nothing to compare with
one). Exit code is 0 when every pair's every compared target verdict is
EQUIVALENT, 1 when any pair/target is DIVERGENT or a shared target could
not be compared at all (NOT_COMPARABLE) -- the caller (the shadow
workflow) runs this `continue-on-error: true`, matching every other PR2
check script's own advisory posture, but the exit code itself is real so a
local run or a future promoted gate can act on it.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Tuple

CI_DIR = Path(__file__).resolve().parent
REPO_ROOT = CI_DIR.parent
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

from check_profile import (  # noqa: E402
    CheckProfileError,
    _CODE_SYMBOL_TYPES,
    _load_build_output,
    _public_headers_for_target,
    _soname,
    _target_library_path,
    dynamic_symbols,
)
from select_profiles import SelectionError, load_profiles  # noqa: E402

SCHEMA_VERSION = 1
KIND = "abicheck.cross-build-equivalence/v1"

CAT_PUBLIC_CONTRACT = "public_contract_mismatch"
CAT_TOOLCHAIN = "toolchain_configuration_mismatch"
CAT_EXPECTED_METADATA = "expected_build_system_metadata_difference"
CAT_EVIDENCE_ONLY = "evidence_only_difference"
CAT_UNCLASSIFIED = "unclassified_difference"

# Which categories make a target's own verdict DIVERGENT rather than
# EQUIVALENT -- toolchain/metadata/evidence differences are recorded for
# visibility but don't themselves prove the two builds' public ABI
# surfaces disagree (design doc section 19: "legitimate differences
# include ... generator metadata ... evidence producer details").
_DIVERGENT_CATEGORIES = frozenset({CAT_PUBLIC_CONTRACT, CAT_UNCLASSIFIED})

_NEEDED_RE = re.compile(r"\(NEEDED\)\s*Shared library:\s*\[(.*?)\]")


class CompareBuildOutputsError(RuntimeError):
    pass


def _needed_libraries(library_path: Path) -> List[str]:
    """Sorted `readelf -d` NEEDED entries for library_path -- the runtime
    dynamic dependency list, distinct from _soname()'s own single-entry
    read of the SAME `readelf -d` output (both parse the same command's
    output independently rather than one parsing the other's result, so
    a change to either stays a one-function fix).
    """
    proc = subprocess.run(
        ["readelf", "-d", str(library_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise CheckProfileError(f"readelf -d failed on {library_path}: {proc.stdout}")
    return sorted(_NEEDED_RE.findall(proc.stdout))


def _diff(category: str, aspect: str, detail: str, value_a: Any, value_b: Any) -> Dict[str, Any]:
    return {
        "category": category,
        "aspect": aspect,
        "detail": detail,
        "value_a": value_a,
        "value_b": value_b,
    }


def _compare_symbol_sets(
    symbols_a: List[Dict[str, Any]], symbols_b: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    by_name_a = {s["name"]: s for s in symbols_a}
    by_name_b = {s["name"]: s for s in symbols_b}
    diffs: List[Dict[str, Any]] = []
    only_a = sorted(set(by_name_a) - set(by_name_b))
    only_b = sorted(set(by_name_b) - set(by_name_a))
    if only_a:
        diffs.append(
            _diff(
                CAT_PUBLIC_CONTRACT,
                "exported_dynamic_symbols",
                "symbol(s) exported on one side only",
                only_a,
                [],
            )
        )
    if only_b:
        diffs.append(
            _diff(
                CAT_PUBLIC_CONTRACT,
                "exported_dynamic_symbols",
                "symbol(s) exported on one side only",
                [],
                only_b,
            )
        )
    changed = []
    # Function/code symbol *size* is deliberately excluded from this
    # comparison -- ci/check_profile.py's own dynamic_symbols() docstring
    # notes recompiled code size varies normally by toolchain/optimization
    # and is not itself ABI-relevant; only a data symbol's size changing
    # is. A T/t/W/w/i (code) type change between two build systems' compile
    # of the identical source would still be a real, reportable ABI
    # difference, so `type` is always compared. Reuses
    # ci/check_profile.py's own _CODE_SYMBOL_TYPES (which includes "i",
    # GNU IFUNC -- man nm: the resolver's own implementation-defined code
    # size varies by build-system/compiler flags without that being an
    # ABI break) rather than a locally redefined, incomplete copy that
    # dropped "i" and would misreport an IFUNC's own varying resolver
    # code size as a data-object ABI divergence (CodeRabbit/Codex review,
    # PR #23).
    _CODE_TYPES = _CODE_SYMBOL_TYPES
    for name in sorted(set(by_name_a) & set(by_name_b)):
        a, b = by_name_a[name], by_name_b[name]
        if a["type"] != b["type"]:
            changed.append({"name": name, "aspect": "type", "value_a": a["type"], "value_b": b["type"]})
            continue
        if a["type"] not in _CODE_TYPES and a["size"] != b["size"]:
            changed.append({"name": name, "aspect": "size", "value_a": a["size"], "value_b": b["size"]})
    if changed:
        diffs.append(
            _diff(
                CAT_PUBLIC_CONTRACT,
                "exported_dynamic_symbols",
                "symbol(s) present on both sides with a different type/size",
                changed,
                changed,
            )
        )
    return diffs


def _compare_headers(
    headers_a: Dict[str, str], headers_b: Dict[str, str]
) -> List[Dict[str, Any]]:
    # Both dicts are keyed by staged-relative path (e.g.
    # "headers/include/abicheck_lab/math.h" on the bazel side vs the same
    # relative path under a different staged_dir root on the cmake side --
    # _public_headers_for_target() already normalizes to a path relative
    # to each side's OWN staged_dir, so the keys are directly comparable).
    diffs: List[Dict[str, Any]] = []
    only_a = sorted(set(headers_a) - set(headers_b))
    only_b = sorted(set(headers_b) - set(headers_a))
    if only_a or only_b:
        diffs.append(
            _diff(
                CAT_UNCLASSIFIED,
                "public_header_set",
                "staged public header set differs -- both profiles declare the same "
                "header_roots over the same repo tree, so this should be structurally "
                "impossible and likely indicates a staging bug, not a build-system "
                "difference",
                only_a,
                only_b,
            )
        )
    changed = sorted(
        name
        for name in set(headers_a) & set(headers_b)
        if headers_a[name] != headers_b[name]
    )
    if changed:
        diffs.append(
            _diff(
                CAT_UNCLASSIFIED,
                "public_header_content",
                "staged public header(s) present on both sides with a different "
                "sha256 -- both profiles stage a literal copy of the same repo "
                "file, so this indicates a staging bug (a stale/partial copy), "
                "not a legitimate build-system difference",
                changed,
                changed,
            )
        )
    return diffs


def _try_abicheck_compare(
    target: str,
    library_a: Path,
    headers_a: List[Path],
    library_b: Path,
    headers_b: List[Path],
) -> Dict[str, Any]:
    """Best-effort real-scanner cross-check (see this module's docstring).
    Never raises -- any failure (abicheck not on PATH, a crash, a bad
    report) is recorded as its own outcome, never silently treated as
    "ran and agreed".

    Passes EVERY staged public header for this target on each side (not
    just one) -- every target in this repo happens to declare exactly one
    public header today, but a future target with more than one would
    otherwise have this deep check silently blind to any change outside
    whichever header sorted first (CodeRabbit review, PR #23).
    """
    if shutil.which("abicheck") is None:
        return {"mechanism": "abicheck-compare", "outcome": "not_run", "reason": "abicheck not on PATH"}
    with tempfile.TemporaryDirectory(prefix="abicheck-cross-") as tmp:
        out_path = Path(tmp) / f"{target}.json"
        cmd = [
            "abicheck",
            "compare",
            str(library_a),
            str(library_b),
            "--version",
            "old=profile-a",
            "--version",
            "new=profile-b",
            "--lang",
            "c++",
            "--format",
            "json",
            "-o",
            str(out_path),
            "--policy",
            "strict_abi",
        ]
        for header_a in headers_a:
            cmd += ["--header", f"old={header_a}"]
        for header_b in headers_b:
            cmd += ["--header", f"new={header_b}"]
        try:
            # Deliberately not check=True: a real ABI difference between
            # the two sides is expected to exit non-zero (matching
            # scripts/run_scenario.py's own reasoning for the same CLI) --
            # the report's own verdict field is what this reads, not the
            # process exit code.
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"mechanism": "abicheck-compare", "outcome": "error", "reason": str(exc)}
        try:
            report = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "mechanism": "abicheck-compare",
                "outcome": "error",
                "reason": f"no readable report ({exc}); process output: {proc.stdout[-2000:]}",
            }
        verdict = report.get("verdict")
        return {"mechanism": "abicheck-compare", "outcome": "ran", "verdict": verdict}


def _compiler_context_diffs(compiler_a: Dict[str, Any], compiler_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []
    for field, aspect in (("family", "compiler_family"), ("standard", "cxx_standard")):
        val_a, val_b = compiler_a.get(field), compiler_b.get(field)
        if val_a != val_b:
            diffs.append(
                _diff(
                    CAT_TOOLCHAIN,
                    aspect,
                    f"{aspect} differs between profiles pinned to the same toolchain intent "
                    "in ci/profiles.yaml -- this should not happen for any two profiles "
                    "this repo currently declares",
                    val_a,
                    val_b,
                )
            )
    return diffs


def _metadata_context_diffs(profile_a: Dict[str, Any], profile_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []
    for field in ("backend", "generator", "root"):
        val_a, val_b = profile_a.get(field), profile_b.get(field)
        if val_a != val_b:
            diffs.append(
                _diff(
                    CAT_EXPECTED_METADATA,
                    field,
                    f"{field} differs -- expected for two different build systems, informational only",
                    val_a,
                    val_b,
                )
            )
    return diffs


def compare_target(
    target: str,
    staged_a: Path,
    build_output_a: Dict[str, Any],
    staged_b: Path,
    build_output_b: Dict[str, Any],
    run_abicheck: bool = True,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"target": target, "differences": [], "abicheck_cross_check": None}

    try:
        library_a = _target_library_path(staged_a, build_output_a, target)
        library_b = _target_library_path(staged_b, build_output_b, target)
    except CheckProfileError as exc:
        result.update(verdict="NOT_COMPARABLE", reason=str(exc))
        return result

    try:
        symbols_a = dynamic_symbols(library_a)
        symbols_b = dynamic_symbols(library_b)
        soname_a = _soname(library_a)
        soname_b = _soname(library_b)
        needed_a = _needed_libraries(library_a)
        needed_b = _needed_libraries(library_b)
        headers_a = _public_headers_for_target(staged_a, build_output_a, target)
        headers_b = _public_headers_for_target(staged_b, build_output_b, target)
    except CheckProfileError as exc:
        result.update(verdict="NOT_COMPARABLE", reason=str(exc))
        return result

    diffs = list(_compare_symbol_sets(symbols_a, symbols_b))
    if soname_a != soname_b:
        diffs.append(_diff(CAT_PUBLIC_CONTRACT, "soname", "SONAME differs", soname_a, soname_b))
    if needed_a != needed_b:
        diffs.append(
            _diff(
                CAT_TOOLCHAIN,
                "dynamic_dependencies",
                "NEEDED entries differ -- usually a linker-default difference "
                "(e.g. --as-needed behavior) between build systems, not by itself "
                "an ABI break, but recorded since it can affect whether the "
                "library loads standalone",
                needed_a,
                needed_b,
            )
        )
    diffs.extend(_compare_headers(headers_a, headers_b))

    if run_abicheck:
        # Every staged header path for this target on each side (headers
        # dict is {staged-relative path: sha256}) -- not just the first
        # one alphabetically. A target with more than one public header
        # would otherwise leave every header past the first entirely
        # unchecked by this deep comparison (CodeRabbit review, PR #23).
        headers_a_paths = [staged_a / name for name in sorted(headers_a)]
        headers_b_paths = [staged_b / name for name in sorted(headers_b)]
        cross = _try_abicheck_compare(target, library_a, headers_a_paths, library_b, headers_b_paths)
        result["abicheck_cross_check"] = cross
        if cross.get("outcome") == "ran" and cross.get("verdict") not in ("NO_CHANGE", "COMPATIBLE"):
            diffs.append(
                _diff(
                    CAT_PUBLIC_CONTRACT,
                    "abicheck_cross_check",
                    f"real abicheck compare between the two profiles' own built libraries "
                    f"reported verdict={cross.get('verdict')!r} -- a source/DWARF-level "
                    "difference the dynamic-symbol-table diff above cannot see",
                    None,
                    None,
                )
            )

    result["differences"] = diffs
    if any(d["category"] in _DIVERGENT_CATEGORIES for d in diffs):
        result["verdict"] = "DIVERGENT"
    else:
        result["verdict"] = "EQUIVALENT"
    return result


def compare_pair(
    profile_id_a: str,
    staged_a: Path,
    profile_id_b: str,
    staged_b: Path,
    canonical_targets: List[str],
    run_abicheck: bool = True,
) -> Dict[str, Any]:
    build_output_a = _load_build_output(staged_a, prefer_standard=True)
    build_output_b = _load_build_output(staged_b, prefer_standard=True)

    # canonical_targets is the shared_library subset of ci/profiles.yaml's
    # `coverage.checked_targets` common to BOTH profiles being compared --
    # NOT every target either side's build-output.json happens to mark
    # kind: shared_library. That distinction matters: the bazel profile
    # also builds //:math_shared, a real cc_shared_library alongside
    # //:math's cc_binary(linkshared=True) demo shape (BUILD.bazel's own
    # comment: "Legacy/demo target shape" vs "Realistic target shape") --
    # a Bazel-only capability the design doc is explicit CMake/Make must
    # never be forced to emulate (section 11 item 9/10, section 16
    # "Build-system-specific diagnostics"). Comparing against a
    # kind-inferred set previously flagged math_shared's mere existence on
    # the bazel side as a "one profile built a target the other didn't"
    # finding -- a false positive about an intentional, documented
    # Bazel-only target, not a real cross-build gap.
    target_results = {
        name: compare_target(name, staged_a, build_output_a, staged_b, build_output_b, run_abicheck=run_abicheck)
        for name in canonical_targets
    }

    context_diffs = _compiler_context_diffs(
        build_output_a.get("compiler", {}), build_output_b.get("compiler", {})
    ) + _metadata_context_diffs(build_output_a.get("profile", {}), build_output_b.get("profile", {}))

    # Extra shared-library targets either side built beyond the canonical
    # set (e.g. bazel's math_shared) ARE still recorded -- just as expected
    # build-system metadata, informational only, never as an unclassified
    # or public-contract finding, and never excluded silently (design
    # doc's own "no silent shrinking of the matrix" principle applies to
    # what this script chooses not to compare too).
    all_shared_a = {n for n, t in build_output_a.get("targets", {}).items() if t.get("kind") == "shared_library"}
    all_shared_b = {n for n, t in build_output_b.get("targets", {}).items() if t.get("kind") == "shared_library"}
    extra_a = sorted(all_shared_a - set(canonical_targets))
    extra_b = sorted(all_shared_b - set(canonical_targets))
    if extra_a or extra_b:
        context_diffs.append(
            _diff(
                CAT_EXPECTED_METADATA,
                "build_system_specific_targets",
                "shared-library target(s) built by one profile outside the canonical "
                "checked_targets set (e.g. a Bazel-only cc_shared_library capability) -- "
                "not compared, recorded for context only",
                extra_a,
                extra_b,
            )
        )

    if not canonical_targets:
        verdict = "NOT_COMPARABLE"
    elif any(r["verdict"] == "NOT_COMPARABLE" for r in target_results.values()):
        verdict = "NOT_COMPARABLE"
    elif any(r["verdict"] == "DIVERGENT" for r in target_results.values()):
        verdict = "DIVERGENT"
    else:
        verdict = "EQUIVALENT"

    return {
        "profile_a": profile_id_a,
        "profile_b": profile_id_b,
        "backend_a": build_output_a.get("profile", {}).get("backend"),
        "backend_b": build_output_b.get("profile", {}).get("backend"),
        "targets": target_results,
        "context_differences": context_diffs,
        "verdict": verdict,
    }


def _canonical_targets(
    profile_id_a: str, profile_id_b: str, declared_profiles: Dict[str, Any]
) -> List[str]:
    """The `coverage.checked_targets` ci/profiles.yaml declares for BOTH
    profile_id_a and profile_id_b, intersected -- see compare_pair()'s own
    comment for why this, not a build-output.json kind-inference, is the
    canonical comparison set. A profile id this manifest doesn't declare
    (e.g. an ad-hoc local --build-dir with a made-up id) falls back to an
    empty checked_targets list rather than raising, so a local experiment
    still gets a (empty, honestly NOT_COMPARABLE) result instead of a
    crash -- the shadow workflow's own profile ids always come from
    ci/profiles.yaml, so this fallback path is a local-usability
    accommodation, not something CI is expected to hit.
    """
    checked_a = set((declared_profiles.get(profile_id_a) or {}).get("coverage", {}).get("checked_targets", []))
    checked_b = set((declared_profiles.get(profile_id_b) or {}).get("coverage", {}).get("checked_targets", []))
    return sorted(checked_a & checked_b)


def compare_all(
    build_dirs: Dict[str, Path],
    profiles_path: Path = CI_DIR / "profiles.yaml",
    run_abicheck: bool = True,
) -> Dict[str, Any]:
    if len(build_dirs) < 2:
        raise CompareBuildOutputsError(
            f"need at least two --build-dir entries to compare, got {len(build_dirs)}"
        )

    declared_profiles = load_profiles(profiles_path)

    shas = set()
    for profile_id, staged_dir in build_dirs.items():
        build_output = _load_build_output(staged_dir, prefer_standard=True)
        sha = build_output.get("git", {}).get("sha")
        if sha and sha != "unknown":
            shas.add(sha)
    # A pairwise comparison of two profiles built from different commits
    # proves nothing about cross-build-system equivalence -- it would just
    # be re-discovering that the source changed between builds. Not fatal
    # (a local ad-hoc run comparing stale artifacts is still useful to a
    # human), but surfaced loudly rather than silently compared as if the
    # commits matched.
    commit_diagnostic = None
    if len(shas) > 1:
        commit_diagnostic = (
            f"build-dir(s) were staged from {len(shas)} different git commits ({sorted(shas)}) -- "
            "cross-build-equivalence is only meaningful for the same source commit"
        )

    pairs = [
        compare_pair(
            id_a,
            build_dirs[id_a],
            id_b,
            build_dirs[id_b],
            _canonical_targets(id_a, id_b, declared_profiles),
            run_abicheck=run_abicheck,
        )
        for id_a, id_b in combinations(sorted(build_dirs), 2)
    ]

    if any(p["verdict"] == "NOT_COMPARABLE" for p in pairs):
        overall = "INCOMPLETE"
    elif any(p["verdict"] == "DIVERGENT" for p in pairs):
        overall = "DIVERGENT"
    else:
        overall = "EQUIVALENT"

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "advisory": True,
        "profiles_compared": sorted(build_dirs),
        "commit_diagnostic": commit_diagnostic,
        "pairs": pairs,
        "overall_verdict": overall,
    }


def render_markdown(doc: Dict[str, Any]) -> str:
    lines = [
        "# Cross-build-system equivalence (advisory)",
        "",
        f"Overall verdict: **{doc['overall_verdict']}**",
        "",
        f"Profiles compared: {', '.join(doc['profiles_compared'])}",
        "",
    ]
    if doc.get("commit_diagnostic"):
        lines += [f"> ⚠️ {doc['commit_diagnostic']}", ""]
    lines += ["| Pair | Target | Verdict | Findings |", "| --- | --- | --- | --- |"]
    for pair in doc["pairs"]:
        pair_label = f"{pair['profile_a']} ↔ {pair['profile_b']}"
        if not pair["targets"]:
            lines.append(f"| {pair_label} | - | {pair['verdict']} | no shared target |")
        for name, target_result in pair["targets"].items():
            if target_result["verdict"] == "NOT_COMPARABLE":
                findings = target_result.get("reason", "")
            else:
                real = [d for d in target_result["differences"] if d["category"] in _DIVERGENT_CATEGORIES]
                findings = "; ".join(f"{d['aspect']} ({d['category']})" for d in real) or "none"
            lines.append(f"| {pair_label} | {name} | {target_result['verdict']} | {findings} |")
        for d in pair["context_differences"]:
            lines.append(f"| {pair_label} | (context) | {d['category']} | {d['aspect']}: {d['detail']} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _parse_build_dir_arg(raw: str) -> Tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected PROFILE_ID=PATH, got {raw!r}")
    profile_id, _, path = raw.partition("=")
    if not profile_id or not path:
        raise argparse.ArgumentTypeError(f"expected PROFILE_ID=PATH, got {raw!r}")
    return profile_id, Path(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--build-dir",
        action="append",
        default=[],
        required=True,
        metavar="PROFILE_ID=PATH",
        help="A staged abicheck-build-<profile-id>/ directory, repeatable (at least twice)",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, default=None)
    parser.add_argument("--profiles-file", type=Path, default=CI_DIR / "profiles.yaml")
    parser.add_argument(
        "--no-abicheck",
        action="store_true",
        help="skip the optional real-scanner cross-check even when abicheck is on PATH",
    )
    args = parser.parse_args(argv)

    build_dirs: Dict[str, Path] = {}
    for raw in args.build_dir:
        profile_id, path = _parse_build_dir_arg(raw)
        if profile_id in build_dirs:
            print(f"compare_build_outputs: duplicate --build-dir profile id {profile_id!r}", file=sys.stderr)
            return 1
        if not (path / "lab-build-output.json").is_file() and not (path / "build-output.json").is_file():
            print(f"compare_build_outputs: {path} has no build-output.json (profile {profile_id!r})", file=sys.stderr)
            return 1
        build_dirs[profile_id] = path

    try:
        doc = compare_all(build_dirs, profiles_path=args.profiles_file, run_abicheck=not args.no_abicheck)
    except (CompareBuildOutputsError, CheckProfileError, SelectionError) as exc:
        print(f"compare_build_outputs: {exc}", file=sys.stderr)
        return 1

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    markdown = render_markdown(doc)
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(markdown, encoding="utf-8")
    print(markdown)

    return 0 if doc["overall_verdict"] == "EQUIVALENT" else 1


if __name__ == "__main__":
    sys.exit(main())
