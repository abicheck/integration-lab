#!/usr/bin/env python3
"""Compare the actual built wheels, not a standalone extension.

`ci/run_python_extension_scenario.py` recompiles a bare `_core.so` and
compares adjacent `.pyi` files.  That is a sound unit-level acceptance case
and it stays, but it is not wheel integration: it never exercises the
packaging layer at all.  Nothing in it would notice if the extension were
missing from the wheel, if the `.pyi` were not packaged, if a second
extension appeared or vanished, or if the wheel's tags stopped matching the
interpreter that will install it.

This scenario compares `old.whl` against `new.whl` and asserts what only a
package-level comparison can see:

* the extension is discovered *inside the package*, at its packaged path;
* the `.pyi` is discovered inside the package (the Python API findings can
  only come from a stub that was actually packaged);
* the Python API findings themselves;
* bundled native library discovery, via the bundle verdict/findings;
* package-level added/removed extensions, via unmatched_old/unmatched_new;
* wheel tags and CPython ABI metadata, read from the wheel's own filename
  and `.dist-info/WHEEL` rather than assumed.

Post-repair behaviour (auditwheel/delocate/delvewheel) is not covered here
and is declared as a follow-up in docs/roadmap.md rather than implied.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from scenario_command import run_command

#: `name-version-pytag-abitag-platformtag.whl` (PEP 427/425).
WHEEL_NAME_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>.+?)-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)


class ScenarioError(RuntimeError):
    """The scenario could not be run as declared."""


# --------------------------------------------------------------------------
# Wheel identity
# --------------------------------------------------------------------------


def parse_wheel_tags(wheel: Path) -> Dict[str, str]:
    match = WHEEL_NAME_RE.match(wheel.name)
    if match is None:
        raise ScenarioError(f"{wheel.name}: not a PEP 427 wheel filename")
    return match.groupdict()


def read_wheel_metadata(wheel: Path) -> Dict[str, Any]:
    """Packaged contents plus the `.dist-info/WHEEL` tag block."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        wheel_files = [n for n in names if n.endswith(".dist-info/WHEEL")]
        if not wheel_files:
            raise ScenarioError(f"{wheel.name}: no .dist-info/WHEEL in the archive")
        try:
            raw = archive.read(wheel_files[0]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScenarioError(f"{wheel.name}: WHEEL metadata is not UTF-8: {exc}") from exc
    tags = []
    root_is_purelib = None
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "tag":
            tags.append(value)
        elif key == "root-is-purelib":
            root_is_purelib = value.lower() == "true"
    return {
        "contents": sorted(names),
        "extensions": sorted(n for n in names if n.endswith((".so", ".pyd", ".dylib"))),
        "stubs": sorted(n for n in names if n.endswith(".pyi")),
        "tags": sorted(tags),
        "root_is_purelib": root_is_purelib,
    }


def assert_wheel_identity(
    old: Path, new: Path, expected: Dict[str, Any]
) -> List[str]:
    """Both wheels must target the same interpreter/ABI/platform."""
    errors = []
    old_tags, new_tags = parse_wheel_tags(old), parse_wheel_tags(new)
    for field in ("python", "abi", "platform"):
        if old_tags[field] != new_tags[field]:
            errors.append(
                f"wheel {field} tag differs between sides: "
                f"{old_tags[field]!r} vs {new_tags[field]!r}; the comparison "
                "would be across two different build targets"
            )
    # An extension wheel that came out purelib-tagged means the extension was
    # not packaged as one -- the exact packaging failure a standalone .so
    # comparison cannot see.
    for wheel, meta in ((old, read_wheel_metadata(old)), (new, read_wheel_metadata(new))):
        if not meta["extensions"]:
            errors.append(f"{wheel.name}: no extension module packaged inside the wheel")
        if expected.get("require_stubs") and not meta["stubs"]:
            errors.append(f"{wheel.name}: no .pyi stub packaged inside the wheel")
        if meta["root_is_purelib"]:
            errors.append(
                f"{wheel.name}: Root-Is-Purelib is true, but this wheel ships a "
                "compiled extension"
            )
        if "any" in old_tags["platform"] or "none" == old_tags["abi"]:
            errors.append(
                f"{wheel.name}: tagged {old_tags['abi']}/{old_tags['platform']}, "
                "which is not a CPython-ABI extension wheel"
            )
        for package in expected.get("packaged_under", []):
            if not any(name.startswith(package) for name in meta["extensions"]):
                errors.append(
                    f"{wheel.name}: no extension packaged under {package!r}; "
                    f"extensions found: {meta['extensions']!r}"
                )
    return errors


# --------------------------------------------------------------------------
# Report assertions
# --------------------------------------------------------------------------


def module_stem(name: str) -> str:
    """`_extra.cpython-311-x86_64-linux-gnu.so` -> `_extra`.

    Expectations are declared by module stem, not by filename: the ABI tag
    in an extension filename is whatever interpreter the runner happens to
    have, so a filename-level expectation would pin this scenario to one
    Python version and fail on every other.
    """
    base = Path(name).name
    return base.split(".", 1)[0]


def _findings(report: Dict[str, Any]) -> set:
    found = set()
    for library in report.get("libraries") or []:
        for finding in library.get("findings") or []:
            found.add((finding.get("kind"), finding.get("symbol")))
    return found


def assert_report(report: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    errors = []
    if report.get("verdict") != expected["verdict"]:
        errors.append(f"verdict={report.get('verdict')!r}, expected {expected['verdict']!r}")

    declared = {(f["kind"], f["symbol"]) for f in expected.get("findings", [])}
    observed = _findings(report)
    if observed != declared:
        errors.append(f"findings={sorted(observed)!r}, expected {sorted(declared)!r}")

    # Package-level extension accounting: an extension appearing or
    # disappearing from the package is invisible to a standalone .so compare.
    for key in ("unmatched_old", "unmatched_new"):
        want = sorted(expected.get(key, []))
        got = sorted(module_stem(name) for name in (report.get(key) or []))
        if got != want:
            errors.append(f"{key} modules={got!r}, expected {want!r}")

    if "changed_libraries" in expected:
        want = sorted(expected["changed_libraries"])
        got = sorted(module_stem(name) for name in (report.get("changed_libraries") or []))
        if got != want:
            errors.append(f"changed_libraries modules={got!r}, expected {want!r}")

    if "bundle_verdict" in expected and report.get("bundle_verdict") != expected["bundle_verdict"]:
        errors.append(
            f"bundle_verdict={report.get('bundle_verdict')!r}, "
            f"expected {expected['bundle_verdict']!r}"
        )
    declared_bundle = {(f["kind"], f["symbol"]) for f in expected.get("bundle_findings", [])}
    observed_bundle = {
        (f.get("kind"), module_stem(f.get("symbol") or ""))
        for f in (report.get("bundle_findings") or [])
    }
    if observed_bundle != declared_bundle:
        errors.append(
            f"bundle_findings={sorted(observed_bundle)!r}, expected {sorted(declared_bundle)!r}"
        )
    return errors


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build_wheel(package_root: Path, stub: Optional[Path], out_dir: Path, work: Path) -> Path:
    """Build one wheel from a copy of the package, optionally restubbed."""
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(package_root, work)
    if stub is not None:
        target = work / "abicheck_lab_py" / "_core.pyi"
        if not target.parent.is_dir():
            raise ScenarioError(f"{work}: package directory not found for stub install")
        shutil.copy2(stub, target)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_command([
        sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet",
        str(work), "-w", str(out_dir),
    ])
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ScenarioError(f"{out_dir}: expected exactly one wheel, found {len(wheels)}")
    return wheels[0]


def add_extension_copy(wheel: Path, out_dir: Path, new_stem: str) -> Path:
    """Repack `wheel` with one extra extension module inside the package.

    Deterministic way to exercise package-level added/removed accounting
    without shipping a second module in the real package: the added file is
    a byte copy of the existing extension under a different module name, so
    the only difference the comparison can see is the extension's presence.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / wheel.name
    shutil.copy2(wheel, target)
    with zipfile.ZipFile(wheel) as archive:
        extensions = [n for n in archive.namelist() if n.endswith(".so")]
        if not extensions:
            raise ScenarioError(f"{wheel.name}: no extension to copy")
        source_name = extensions[0]
        payload = archive.read(source_name)
    added = source_name.replace("_core.", f"{new_stem}.")
    if added == source_name:
        raise ScenarioError(f"{source_name}: could not derive a second module name")
    with zipfile.ZipFile(target, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(added, payload)
    return target


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def run(manifest: Path, output: Path) -> List[str]:
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    try:
        scenario = next(
            item for item in doc.get("wheel_scenarios", [])
            if item.get("id") == "wheel-package-level"
        )
    except StopIteration:
        raise ScenarioError(f"{manifest}: no wheel scenario declared") from None

    root = manifest.parents[1]
    package_root = root / scenario["package"]
    stubs = root / scenario["stubs"]
    output.mkdir(parents=True, exist_ok=True)

    wheels = {}
    for side, stub_name in (("old", scenario["old_stub"]), ("new", scenario["new_stub"])):
        wheels[side] = build_wheel(
            package_root, stubs / stub_name, output / f"{side}-wheel", output / f"{side}-src"
        )

    errors = assert_wheel_identity(
        wheels["old"], wheels["new"], scenario.get("identity", {})
    )

    summary = {}
    for case in scenario["cases"]:
        case_id = case["id"]
        old = wheels[case.get("old", "old")]
        if case.get("new") == "old_plus_extension":
            new = add_extension_copy(old, output / f"{case_id}-plus", case["added_module"])
        else:
            new = wheels[case.get("new", "new")]
        report_path = output / f"{case_id}-report.json"
        run_command(
            ["abicheck", "compare", str(old), str(new), "--format", "json",
             "-o", str(report_path)],
            verdict_report=report_path,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        case_errors = assert_report(report, case["expect"])
        summary[case_id] = case_errors or "ok"
        errors.extend(f"{case_id}: {error}" for error in case_errors)

    summary["wheel_tags"] = parse_wheel_tags(wheels["old"])
    summary["packaged"] = read_wheel_metadata(wheels["old"])["extensions"]
    (output / "wheel-comparison.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("scenarios/manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("python-wheel-results"))
    args = parser.parse_args(argv)
    try:
        errors = run(args.manifest, args.output)
    except (OSError, ScenarioError, RuntimeError, KeyError,
            zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
