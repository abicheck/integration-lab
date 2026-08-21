#!/usr/bin/env python3
"""PR2: run a real ABI comparison against one profile's staged build output
(`abicheck-build-<profile-id>/`, produced by `ci/run_profile.py`/
`ci/emit_build_output.py`) and against a committed baseline
(`abi/profiles/<profile-id>/<target>.abicheck.json`).

## What this actually is, and what it is not

The real `abicheck` scanner (`pip install abicheck @ git+https://github.com/
abicheck/abicheck.git@...`, the same package `.github/workflows/abi-scan.yml`
installs) is not available in this sandbox -- there is no network access here
(verified: `pip download` against that same git+https URL times out with no
response), and nothing under `tools/abicheck/` is a vendored copy of the
scanner itself. `tools/abicheck/facts.bzl`/`BUILD.bazel` are Bazel-only
*glue* that feeds a source-fact pack to the real, externally-installed
`abicheck` CLI (see that CLI's `--build-info` flag) -- they do not implement
comparison logic on their own, and they only apply to Bazel-built targets in
the first place.

So this script is deliberately NOT a reimplementation of, wrapper around, or
stand-in claiming to be `abicheck`. It is a small, honest, source-of-truth
ABI *compatibility signal* built directly from the ELF binary and toolchain
utilities every one of this lab's runners already has (`nm`, `readelf` --
part of `binutils`, present on every Ubuntu runner and this sandbox, no
install step needed): a diff of each staged shared library's **dynamic
symbol table** (the same table the real `abicheck` also ultimately anchors
its binary-level evidence to) between a committed baseline and this profile's
freshly-built candidate, plus a content-hash diff of the target's staged
public headers.

## What this catches, and what it structurally cannot

Catches, reliably: an exported function/global-data symbol being removed
(the single most common breaking ABI change -- `fixtures/remove_function/`
is exactly this), a symbol's ELF type or size changing (e.g. a global
variable resized), a symbol newly exported (compatible, additive), and the
public header set changing at all (surfaced as a note, always -- see below).

Cannot catch, by construction, and this is the honest limitation to record
(see `UPSTREAM_TO_ABICHECK.md`'s PR2 entry): anything invisible at the
dynamic-symbol-table level -- a struct/class layout change with no symbol
rename, a default-argument change, an inline function body change, a
template instantiation change, a C++ overload's parameter types changing in
a way that happens to keep the same mangled name (impossible for a real
mangled C++ symbol, but the point generalizes: this is a binary-surface
diff, not a source-level AST/DWARF comparison). Real `abicheck` does the
latter; this script does the former. `public_headers_changed` in the report
is the honest signal for "something in the API surface changed that this
mechanism cannot itself classify" -- it is surfaced, never silently dropped,
and never folded into the verdict on its own (a header comment/whitespace
change would otherwise falsely read as a break).

## Modes

    ci/check_profile.py dump  --profile-id ID --target math --staged-dir DIR --out PATH
    ci/check_profile.py check --profile-id ID --target math --staged-dir DIR --baseline PATH --out PATH

`dump` reads a profile's staged output and writes a normalized baseline
(`abicheck.lab-symbol-baseline/v1`) -- this is how every
`abi/profiles/<id>/<target>.abicheck.json` baseline this PR commits was
produced (see that directory's own README note and this file's own
`--repo-root-marker`-free design: unlike `scripts/normalize_baseline.py`,
this schema is deterministic by construction -- no timestamps, no absolute
paths, no per-run metadata -- so there is nothing to normalize away).

`check` reads a baseline and a fresh staged build, and writes a report
(`abicheck.lab-symbol-report/v1`) with a `verdict` of `NO_CHANGE`,
`COMPATIBLE`, `BREAKING`, or `NOT_COMPARABLE`. Exit code is 0 for
`NO_CHANGE`/`COMPATIBLE`, 1 otherwise -- matching the existing
`check_coverage_contract.py` gate convention in this repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA_VERSION = 1
BASELINE_KIND = "abicheck.lab-symbol-baseline/v1"
REPORT_KIND = "abicheck.lab-symbol-report/v1"

MECHANISM_NOTE = (
    "nm/readelf dynamic-symbol-table + public-header-digest diff (lab-only "
    "simplification -- NOT the full abicheck source-level ABI scanner, which "
    "is unavailable in this sandbox; see this script's own module docstring "
    "and UPSTREAM_TO_ABICHECK.md's PR2 entry)"
)

# ELF/linker-internal dynamic symbols that show up in every shared object's
# .dynsym regardless of what the library's own source exports -- never part
# of a library's own public ABI. The first three are exactly the ones the
# real, committed abi/math.abicheck.json baseline classifies as
# "non_public_symbol_to_reason": "internal_or_private_export" (verified
# against that file); the rest are the same class of glibc/gcc-runtime
# linker artifact, added here because this script's own local build (gcc-14,
# not abicheck's conda-forge toolchain) was verified to emit them too.
_NON_PUBLIC_SYMBOL_NAMES = frozenset({
    "__bss_start",
    "_edata",
    "_end",
    "_init",
    "_fini",
    "_ITM_registerTMCloneTable",
    "_ITM_deregisterTMCloneTable",
    "__cxa_finalize",
    "__gmon_start__",
    "__deregister_frame_info",
    "__register_frame_info",
})

# nm -D --defined-only -P output columns: "name type value size" (value/size
# in hex, size only present for object/function symbols with a known size).
# Uppercase type letter = GLOBAL binding, lowercase = LOCAL -- -D
# --defined-only already excludes undefined (U) and, in practice, local
# symbols aren't emitted into .dynsym at all, but the type is still recorded
# verbatim (not just "exported") since a T->W (function->weak) change is
# itself a real ABI-relevant signal this diff should not silently collapse.
_NM_LINE_RE = re.compile(r"^(?P<name>\S+)\s+(?P<type>[A-Za-z])(?:\s+(?P<value>[0-9a-fA-F]+))?(?:\s+(?P<size>[0-9a-fA-F]+))?$")

# nm type letters for code (T/t text, W/w weak -- almost always a function
# in a dynamic symbol table): a changed *code size* here is normal for any
# recompiled function body (a bugfix, an optimization level change, a
# compiler version bump) and is not itself an ABI break -- unlike a changed
# *data* symbol's size (D/d/B/b/R/r/G/g/S/s), which really is ABI-relevant
# (a struct/array a consumer links against grew or shrank). Comparing size
# for every symbol regardless of type flagged ordinary recompiles as
# BREAKING (Codex review, PR #20); type changes are still always compared.
_CODE_SYMBOL_TYPES = frozenset({"T", "t", "W", "w"})


class CheckProfileError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_build_output(staged_dir: Path) -> Dict[str, Any]:
    path = staged_dir / "build-output.json"
    if not path.is_file():
        raise CheckProfileError(f"no build-output.json under {staged_dir} -- run ci/run_profile.py first")
    return _load_json(path)


def _target_library_path(staged_dir: Path, build_output: Dict[str, Any], target: str) -> Path:
    targets = build_output.get("targets", {})
    entry = targets.get(target)
    if entry is None:
        raise CheckProfileError(f"build-output.json has no target {target!r} (known: {sorted(targets)})")
    if not entry.get("built") or not entry.get("path"):
        raise CheckProfileError(f"target {target!r} was not built (see build-output.json diagnostics)")
    path = staged_dir / entry["path"]
    if not path.is_file():
        raise CheckProfileError(f"staged artifact for {target!r} missing on disk: {path}")
    return path


def dynamic_symbols(library_path: Path) -> List[Dict[str, Any]]:
    """Real `nm -D --defined-only` output for `library_path`, parsed into a
    sorted, JSON-serializable list -- never mocked, never a stub table.
    """
    proc = subprocess.run(
        ["nm", "-D", "--defined-only", "-P", str(library_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise CheckProfileError(f"nm -D failed on {library_path}: {proc.stdout}")
    symbols: Dict[str, Dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NM_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if name in _NON_PUBLIC_SYMBOL_NAMES:
            continue
        size = m.group("size")
        symbols[name] = {
            "name": name,
            "type": m.group("type"),
            "size": int(size, 16) if size else None,
        }
    return sorted(symbols.values(), key=lambda s: s["name"])


def _soname(library_path: Path) -> Optional[str]:
    proc = subprocess.run(
        ["readelf", "-d", str(library_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        return None
    m = re.search(r"\(SONAME\)\s*Library soname:\s*\[(.*?)\]", proc.stdout)
    return m.group(1) if m else None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_headers_for_target(staged_dir: Path, build_output: Dict[str, Any], target: str) -> Dict[str, str]:
    """{staged-relative header path: sha256} for every header staged under
    this target's own header root(s) -- `include/` for `math`,
    `strings_lib/include/` for `strings`. Matched by the same simple prefix
    convention `ci/profiles.yaml`'s own `header_roots` already uses per
    profile; the target->root mapping below is this repo's own fixed
    layout (verified against `ci/profiles.yaml` and the real repo tree),
    not a generic inference.
    """
    root_prefix = "strings_lib/include" if target == "strings" else "include"
    headers_dir = staged_dir / "headers" / root_prefix
    result: Dict[str, str] = {}
    if not headers_dir.is_dir():
        return result
    for path in sorted(headers_dir.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(staged_dir))
            result[rel] = _sha256_file(path)
    return result


def build_baseline(profile_id: str, target: str, staged_dir: Path) -> Dict[str, Any]:
    build_output = _load_build_output(staged_dir)

    # Mirror compare_profile()'s own staged-identity guard: `dump` accepts
    # --profile-id and --staged-dir as two independent CLI arguments, so
    # nothing stops a caller from stamping a p2-built staged_dir's baseline
    # as belonging to p1. compare_profile()'s identity check can't catch
    # this after the fact -- a baseline poisoned this way carries
    # self-consistent profile_id/target metadata that matches whatever the
    # (also mismatched) compare-side caller later claims, so the two
    # checks would agree with each other while both being wrong (Codex
    # review, PR #20). Reject it here, at the only point the staged_dir's
    # own recorded identity is actually available to compare against.
    staged_profile_id = build_output.get("profile", {}).get("id")
    if staged_profile_id != profile_id:
        raise CheckProfileError(
            f"staged_dir's own build-output.json was produced by profile "
            f"{staged_profile_id!r}, not the requested profile_id={profile_id!r} -- "
            "refusing to stamp a baseline with the wrong identity"
        )

    library_path = _target_library_path(staged_dir, build_output, target)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": BASELINE_KIND,
        "profile_id": profile_id,
        "target": target,
        "backend": build_output.get("profile", {}).get("backend"),
        "library_filename": library_path.name,
        "soname": _soname(library_path),
        "dynamic_symbols": dynamic_symbols(library_path),
        "public_headers": _public_headers_for_target(staged_dir, build_output, target),
    }


def _index_by_name(symbols: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {s["name"]: s for s in symbols}


def compare_profile(
    profile_id: str,
    target: str,
    staged_dir: Path,
    baseline: Dict[str, Any],
) -> Dict[str, Any]:
    facts: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "profile_id": profile_id,
        "target": target,
        "mechanism": MECHANISM_NOTE,
        "baseline_profile_id": baseline.get("profile_id"),
    }

    if baseline.get("kind") != BASELINE_KIND:
        facts.update(
            verdict="NOT_COMPARABLE",
            detail=f"baseline kind {baseline.get('kind')!r} is not {BASELINE_KIND!r}",
            symbols={"added": [], "removed": [], "changed": [], "unchanged_count": 0},
            public_headers_changed=[],
        )
        return facts

    # Confirm this baseline is actually FOR this profile/target -- kind
    # alone doesn't rule out an accidentally copy-pasted or wrong-flag
    # baseline (e.g. the cmake profile's math baseline passed to a make
    # run, or math's baseline passed for strings). Since the three
    # profiles' math/strings surfaces are nearly identical and function
    # code size is deliberately ignored, a mismatched baseline could
    # otherwise report NO_CHANGE and silently defeat the whole per-profile
    # contract (Codex review, PR #20).
    baseline_profile_id = baseline.get("profile_id")
    baseline_target = baseline.get("target")
    if baseline_profile_id != profile_id or baseline_target != target:
        facts.update(
            verdict="NOT_COMPARABLE",
            detail=(
                f"baseline is for profile_id={baseline_profile_id!r} target={baseline_target!r}, "
                f"not the requested profile_id={profile_id!r} target={target!r}"
            ),
            symbols={"added": [], "removed": [], "changed": [], "unchanged_count": 0},
            public_headers_changed=[],
        )
        return facts

    try:
        build_output = _load_build_output(staged_dir)
    except CheckProfileError as exc:
        facts.update(
            verdict="NOT_COMPARABLE",
            detail=str(exc),
            symbols={"added": [], "removed": [], "changed": [], "unchanged_count": 0},
            public_headers_changed=[],
        )
        return facts

    # The baseline/target identity check above only proves the *caller's*
    # arguments match -- it says nothing about which profile actually
    # produced the staged_dir we're about to read from. staged_dir is a
    # plain CLI argument (e.g. --staged-dir abicheck-build-<profile-id>);
    # nothing stopped a caller from passing --profile-id p-cmake alongside
    # a staged_dir that was actually built by the make profile. Both
    # backends can have matching symbol tables and headers for the same
    # target, so this would report NO_CHANGE against a build that was
    # never actually produced by the profile it claims to be (Codex
    # review, PR #20).
    staged_profile_id = build_output.get("profile", {}).get("id")
    if staged_profile_id != profile_id:
        facts.update(
            verdict="NOT_COMPARABLE",
            detail=(
                f"staged_dir's own build-output.json was produced by profile "
                f"{staged_profile_id!r}, not the requested profile_id={profile_id!r}"
            ),
            symbols={"added": [], "removed": [], "changed": [], "unchanged_count": 0},
            public_headers_changed=[],
        )
        return facts

    try:
        library_path = _target_library_path(staged_dir, build_output, target)
    except CheckProfileError as exc:
        facts.update(
            verdict="NOT_COMPARABLE",
            detail=str(exc),
            symbols={"added": [], "removed": [], "changed": [], "unchanged_count": 0},
            public_headers_changed=[],
        )
        return facts

    candidate_symbols = dynamic_symbols(library_path)
    candidate_headers = _public_headers_for_target(staged_dir, build_output, target)

    base_symbols = _index_by_name(baseline.get("dynamic_symbols", []))
    cand_symbols = _index_by_name(candidate_symbols)

    removed = sorted(set(base_symbols) - set(cand_symbols))
    added = sorted(set(cand_symbols) - set(base_symbols))
    changed = []
    for name in sorted(set(base_symbols) & set(cand_symbols)):
        before, after = base_symbols[name], cand_symbols[name]
        type_changed = before.get("type") != after.get("type")
        # Only a data symbol's size is ABI-relevant; a code symbol's size
        # (its compiled function body) legitimately changes on every
        # ordinary recompile and is not a breaking change on its own.
        size_relevant = before.get("type") not in _CODE_SYMBOL_TYPES
        size_changed = size_relevant and before.get("size") != after.get("size")
        if type_changed or size_changed:
            changed.append({
                "name": name,
                "before": {"type": before.get("type"), "size": before.get("size")},
                "after": {"type": after.get("type"), "size": after.get("size")},
            })
    unchanged_count = len(set(base_symbols) & set(cand_symbols)) - len(changed)

    base_headers = baseline.get("public_headers", {})
    headers_changed = sorted(
        path
        for path in set(base_headers) | set(candidate_headers)
        if base_headers.get(path) != candidate_headers.get(path)
    )

    # A SONAME change (e.g. CMake's SOVERSION 1 -> 2) is breaking on its
    # own even when the symbol table and headers are otherwise identical:
    # every existing consumer's NEEDED entry names the OLD soname, which
    # the new library no longer provides, so it can't be loaded at
    # runtime. candidate_soname was already computed for the receipt but
    # never compared against the baseline's -- symbol/header comparisons
    # alone can (and did) report NO_CHANGE/COMPATIBLE for a soname bump
    # (Codex review, PR #20).
    candidate_soname = _soname(library_path)
    baseline_soname = baseline.get("soname")
    soname_changed = baseline_soname is not None and candidate_soname != baseline_soname

    # Bazel's cc_shared_library outputs carry no SONAME at all (baseline_soname
    # is None), so the check above can never fire for them -- but the loader
    # still has to find the library by its on-disk filename in that case
    # (DT_NEEDED records the filename itself when there's no SONAME to record
    # instead). If //:math started producing librenamed.so with identical
    # symbols and headers, this would previously report NO_CHANGE while
    # existing consumers still look for libmath.so and can't load the
    # candidate at runtime. Only meaningful when there's no SONAME to compare
    # instead -- a SONAME change already governs loadability when one exists,
    # regardless of the on-disk filename (Codex review, PR #20).
    baseline_filename = baseline.get("library_filename")
    filename_changed = (
        baseline_soname is None
        and baseline_filename is not None
        and library_path.name != baseline_filename
    )

    if removed or changed or soname_changed or filename_changed:
        verdict = "BREAKING"
        detail_parts = []
        if removed:
            detail_parts.append(f"{len(removed)} exported symbol(s) removed: {', '.join(removed)}")
        if changed:
            detail_parts.append(f"{len(changed)} exported symbol(s) changed type/size: {', '.join(c['name'] for c in changed)}")
        if soname_changed:
            detail_parts.append(f"SONAME changed: {baseline_soname!r} -> {candidate_soname!r}")
        if filename_changed:
            detail_parts.append(
                f"library has no SONAME and its loader filename changed: {baseline_filename!r} -> {library_path.name!r}"
            )
        detail = "; ".join(detail_parts)
    elif headers_changed:
        # A public header edit that doesn't touch the exported symbol
        # table can still be a real ABI break this mechanism has no way
        # to detect -- e.g. adding a data member to a class, changing a
        # struct's layout/size, or widening an inline function's return
        # type. Previously bundled into "added or headers_changed" ->
        # COMPATIBLE, a claim of proven compatibility this symbol-table-
        # only mechanism cannot actually back up for a header-only edit;
        # the receipt/gate then treated it as clean (exit 0) despite a
        # known-but-unclassified change (Codex review, PR #20). Report
        # NOT_COMPARABLE instead: honest about what wasn't checked,
        # rather than silently vouching for it.
        verdict = "NOT_COMPARABLE"
        detail = (
            f"{len(headers_changed)} public header(s) changed content: "
            f"{', '.join(headers_changed)} -- not classified by this symbol-table-only "
            "mechanism (see module docstring); cannot confirm compatibility"
        )
    elif added:
        verdict = "COMPATIBLE"
        detail = f"{len(added)} new exported symbol(s): {', '.join(added)}"
    else:
        verdict = "NO_CHANGE"
        detail = "dynamic symbol table and public header digests are identical to the baseline"

    facts.update(
        verdict=verdict,
        detail=detail,
        symbols={"added": added, "removed": removed, "changed": changed, "unchanged_count": unchanged_count},
        public_headers_changed=headers_changed,
        candidate_library_path=str(library_path.relative_to(staged_dir)),
        candidate_soname=candidate_soname,
    )
    return facts


def _write_json(doc: Dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    dump_p = sub.add_parser("dump", help="write a baseline from a staged profile build")
    dump_p.add_argument("--profile-id", required=True)
    dump_p.add_argument("--target", required=True)
    dump_p.add_argument("--staged-dir", type=Path, required=True)
    dump_p.add_argument("--out", type=Path, required=True)

    check_p = sub.add_parser("check", help="compare a staged profile build against a committed baseline")
    check_p.add_argument("--profile-id", required=True)
    check_p.add_argument("--target", required=True)
    check_p.add_argument("--staged-dir", type=Path, required=True)
    check_p.add_argument("--baseline", type=Path, required=True)
    check_p.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    try:
        if args.mode == "dump":
            baseline = build_baseline(args.profile_id, args.target, args.staged_dir)
            _write_json(baseline, args.out)
            print(json.dumps({"mode": "dump", "profile_id": args.profile_id, "target": args.target, "out": str(args.out)}))
            return 0

        # check
        if not args.baseline.is_file():
            report = {
                "schema_version": SCHEMA_VERSION,
                "kind": REPORT_KIND,
                "profile_id": args.profile_id,
                "target": args.target,
                "mechanism": MECHANISM_NOTE,
                "verdict": "NOT_COMPARABLE",
                "detail": f"no baseline at {args.baseline}",
                "symbols": {"added": [], "removed": [], "changed": [], "unchanged_count": 0},
                "public_headers_changed": [],
            }
            _write_json(report, args.out)
            print(json.dumps({"mode": "check", "profile_id": args.profile_id, "target": args.target, "verdict": report["verdict"]}))
            return 1

        baseline = _load_json(args.baseline)
        report = compare_profile(args.profile_id, args.target, args.staged_dir, baseline)
        _write_json(report, args.out)
        print(json.dumps({"mode": "check", "profile_id": args.profile_id, "target": args.target, "verdict": report["verdict"]}))
        return 0 if report["verdict"] in ("NO_CHANGE", "COMPATIBLE") else 1
    except CheckProfileError as exc:
        print(f"check_profile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
