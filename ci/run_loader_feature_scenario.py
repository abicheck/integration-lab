#!/usr/bin/env python3
"""Prove detection of emitted loader/runtime feature drift.

The existing runtime-floor scenario covers one class of runtime change
(symbol-version floors) well.  This covers the rest of the classes a
dynamic loader actually cares about, each as a deterministic fixture built
from ONE source with only linker/codegen flags differing:

    DT_RELR introduced / removed
    DT_RPATH <-> DT_RUNPATH
    SysV (.hash) style removed
    SONAME major change
    symbol-version node added / removed
    CET (IBT/SHSTK) enabled / weakened
    static TLS (DF_STATIC_TLS) introduced

Deliberately NOT modelled as a "minimum binutils version".  binutils is a
producer tool: which binutils built an artifact is not what a loader sees
and not what breaks a consumer.  What breaks a consumer is the *emitted
feature* -- a DT_RELR the runtime loader cannot read, an RPATH that stopped
being overridable by LD_LIBRARY_PATH, a lost SysV hash table.  Every case
here is therefore named and asserted by the ELF feature it emits, never by
a toolchain version, and `assert_no_version_floor_vocabulary()` keeps it
that way.

Each case also declares what its two sides must actually EMIT, verified
against `readelf` before any comparison runs.  Without that, a toolchain
that silently ignored a flag (an older linker and `-z pack-relative-relocs`,
say) would produce two identical binaries, no findings, and a green
scenario that had tested nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import yaml

from scenario_command import run_command

#: Finding kinds this scenario is responsible for.  A finding whose kind
#: matches one of these must be declared by the case that produced it --
#: that is what stops a loader signal being silently gained or lost.
#: Incidental symbol/DT_NEEDED churn caused by a codegen flag (a TLS model
#: change moves __tls_get_addr, and which GLIBC_* node that lives in varies
#: by glibc) is allowed through and recorded, so the scenario is precise
#: about its own subject without being flaky about the C library's.
LOADER_FEATURE_PATTERNS = (
    re.compile(r"^dt_relr_"),
    re.compile(r"^rpath_"),
    re.compile(r"^runpath_"),
    re.compile(r"^hash_style_"),
    re.compile(r"^soname_"),
    re.compile(r"^symbol_version_(defined|node)_"),
    re.compile(r"^cet_"),
    re.compile(r"^static_tls_"),
)

#: Vocabulary that would mean this scenario had drifted back into modelling
#: a producer-tool version floor instead of an emitted feature.
VERSION_FLOOR_VOCABULARY = re.compile(
    r"binutils|min_linker|minimum_linker|linker_version|toolchain_version",
    re.IGNORECASE,
)


class ScenarioError(RuntimeError):
    """The scenario could not be run as declared."""


def is_loader_feature(kind: str) -> bool:
    return any(pattern.match(kind or "") for pattern in LOADER_FEATURE_PATTERNS)


def assert_no_version_floor_vocabulary(declaration: Any) -> List[str]:
    """Fail if the declaration models a producer-tool version floor."""
    text = yaml.safe_dump(declaration)
    hits = sorted(set(VERSION_FLOOR_VOCABULARY.findall(text)))
    if hits:
        return [
            "loader scenarios must model emitted runtime features, not "
            f"producer-tool version floors; found {hits!r}"
        ]
    return []


def _dynamic_section(binary: Path) -> str:
    """`readelf -d` plus the property notes, as one searchable blob."""
    readelf = shutil.which("readelf")
    if readelf is None:
        raise ScenarioError("readelf is required to verify emitted features")
    out = []
    for args in (["-d", str(binary)], ["-n", str(binary)], ["-V", str(binary)]):
        proc = subprocess.run([readelf, *args], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ScenarioError(f"readelf {' '.join(args)} failed: {proc.stderr.strip()}")
        out.append(proc.stdout)
    return "\n".join(out)


def verify_emitted(binary: Path, emits: Dict[str, Any], side: str) -> List[str]:
    """Check the toolchain really emitted what the case depends on.

    `emits.present` / `emits.absent` are literal substrings of readelf
    output.  A flag the toolchain ignored shows up here as a red scenario
    instead of as a vacuous green comparison of two identical binaries.
    """
    blob = _dynamic_section(binary)
    errors = []
    for needle in emits.get("present", []):
        if needle not in blob:
            errors.append(f"{side}: expected {needle!r} in the emitted ELF, not found")
    for needle in emits.get("absent", []):
        if needle in blob:
            errors.append(f"{side}: expected {needle!r} to be absent, but it was emitted")
    return errors


def _link(cc: str, fixture: Path, output: Path, name: str, flags: List[str]) -> Path:
    binary = output / f"{name}.so"
    resolved = []
    for flag in flags:
        # Version scripts are declared by fixture-relative filename so the
        # manifest never carries an absolute path.
        resolved.append(
            flag.replace("{fixture}", str(fixture.resolve()))
        )
    run_command([cc, "-shared", "-fPIC", str(fixture / "lib.c"), *resolved, "-o", str(binary)])
    return binary


def compare_case(case: Dict[str, Any], fixture: Path, output: Path, cc: str) -> List[str]:
    case_id = case["id"]
    errors: List[str] = []
    snapshots = []
    for side in ("old", "new"):
        spec = case[side]
        binary = _link(cc, fixture, output, f"{case_id}-{side}", spec.get("link", []))
        errors.extend(verify_emitted(binary, spec.get("emits", {}), f"{case_id}/{side}"))
        snapshot = output / f"{case_id}-{side}.abicheck.json"
        run_command(["abicheck", "dump", str(binary), "-o", str(snapshot)])
        snapshots.append(snapshot)
    if errors:
        # A side that did not emit what it claims makes the comparison
        # meaningless; report that rather than a confusing finding diff.
        return errors

    report_path = output / f"{case_id}-report.json"
    run_command(
        ["abicheck", "compare", str(snapshots[0]), str(snapshots[1]),
         "--format", "json", "-o", str(report_path)],
        verdict_report=report_path,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return assert_case(report, case)


def assert_case(report: Dict[str, Any], case: Dict[str, Any]) -> List[str]:
    """Assert the verdict and this case's loader-feature findings exactly."""
    expected = case["expect"]
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(
            f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}"
        )
    if report.get("operational_errors"):
        errors.append(f"operational errors: {report['operational_errors']!r}")

    changes = report.get("changes") or []
    declared = {
        (item["kind"], item["symbol"], item["severity"])
        for item in expected.get("findings", [])
    }
    observed = {
        (change.get("kind"), change.get("symbol"), change.get("severity"))
        for change in changes
        if is_loader_feature(change.get("kind"))
    }
    if observed != declared:
        missing = sorted(declared - observed)
        unexpected = sorted(observed - declared)
        if missing:
            errors.append(f"missing loader findings {missing!r}")
        if unexpected:
            errors.append(f"undeclared loader findings {unexpected!r}")
    return errors


def run(manifest: Path, output: Path, cc: str, only: str | None = None) -> List[str]:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    section = doc.get("loader_scenarios") or []
    if not section:
        raise ScenarioError(f"{manifest}: no loader_scenarios declared")
    errors = assert_no_version_floor_vocabulary(section)
    cases = [case for case in section if only is None or case["id"] == only]
    if not cases:
        raise ScenarioError(f"{manifest}: no loader scenario {only!r}")
    output.mkdir(parents=True, exist_ok=True)
    fixture = manifest.parents[1] / cases[0]["fixture"]

    summary = {}
    for case in cases:
        case_errors = compare_case(case, manifest.parents[1] / case["fixture"], output, cc)
        summary[case["id"]] = case_errors or "ok"
        errors.extend(f"{case['id']}: {error}" for error in case_errors)
    (output / "loader-features.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("loader-feature-results"))
    parser.add_argument("--cc", default=shutil.which("gcc") or "gcc")
    parser.add_argument("--case", default=None, help="run one case id only")
    args = parser.parse_args(argv)
    try:
        errors = run(args.manifest, args.output, args.cc, args.case)
    except (OSError, ScenarioError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
