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
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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


def expand_tags(compressed: str) -> Set[str]:
    """Expand one PEP 425 compressed tag into its concrete tags.

    `cp311.cp312-abi3-linux_x86_64.manylinux_2_17_x86_64` denotes the
    cartesian product of the dot-separated components, which is how both a
    wheel filename and a `.dist-info/WHEEL` Tag line may be written.
    """
    parts = compressed.split("-")
    if len(parts) != 3:
        return set()
    pythons, abis, platforms = (part.split(".") for part in parts)
    return {
        f"{python}-{abi}-{platform}"
        for python in pythons
        for abi in abis
        for platform in platforms
    }


def assert_internal_tags_match_filename(wheel: Path, meta: Dict[str, Any]) -> List[str]:
    """The `.dist-info/WHEEL` Tag lines must be exactly the filename's tags.

    Reading the tags and never checking them is not "identity read from both
    the filename and the packaged metadata", which is what this scenario
    claims to do (Codex review, PR #30).

    Set EQUALITY, not "some tag matches". An earlier version returned as
    soon as one Tag line covered the filename, so a wheel declaring both
    `cp311-cp311-linux_x86_64` and a conflicting `cp999-none-any` passed
    while advertising two incompatible targets (Codex review, second pass).
    A well-formed wheel's filename tag set and its metadata tag set are the
    same set, so anything present on one side and not the other is a real
    inconsistency -- and installers resolve against the metadata, so the
    file that gets installed would not be the one the filename advertises.
    """
    tags = meta.get("tags") or []
    if not tags:
        return [f"{wheel.name}: .dist-info/WHEEL declares no Tag"]
    name = parse_wheel_tags(wheel)
    from_filename = expand_tags(f"{name['python']}-{name['abi']}-{name['platform']}")
    from_metadata: Set[str] = set()
    for tag in tags:
        expanded = expand_tags(tag)
        if not expanded:
            return [f"{wheel.name}: .dist-info/WHEEL Tag {tag!r} is not a PEP 425 tag"]
        from_metadata |= expanded
    if from_metadata == from_filename:
        return []
    only_metadata = sorted(from_metadata - from_filename)
    only_filename = sorted(from_filename - from_metadata)
    detail = []
    if only_metadata:
        detail.append(f"only in .dist-info/WHEEL: {only_metadata!r}")
    if only_filename:
        detail.append(f"only in the filename: {only_filename!r}")
    return [f"{wheel.name}: wheel tags disagree -- " + "; ".join(detail)]


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
        errors.extend(assert_internal_tags_match_filename(wheel, meta))
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


#: Bundle findings that are package-level ACCOUNTING -- an extension
#: appeared or disappeared. Asserted exactly. Everything else a bundle
#: comparison reports (dependency-closure findings, say) is recorded but
#: not pinned: a genuinely loadable second module necessarily links CPython,
#: so its intra-bundle dependency findings are a property of the
#: interpreter it was built against, not of the addition being noticed.
BUNDLE_ACCOUNTING_KINDS = ("bundle_library_added", "bundle_library_removed")


def operational_errors(report: Dict[str, Any]) -> List[Any]:
    """Every operational-error channel a release report can carry.

    `run_command` accepts ABICheck's report-producing exit codes and only
    requires that a report exists, so a partial extraction or comparison can
    still produce the expected verdict. Without this the wheel scenario
    would go green on incomplete evidence, unlike every other runner here
    (Codex review, PR #30). Both levels are read: the release report's own,
    and each per-library entry's.
    """
    found = list(report.get("operational_errors") or [])
    for library in report.get("libraries") or []:
        if not isinstance(library, dict):
            continue
        for error in library.get("operational_errors") or []:
            found.append({"library": library.get("library"), "error": error})
    return found


def assert_report(report: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    errors = []
    problems = operational_errors(report)
    if problems:
        errors.append(f"report contains {len(problems)} operational error(s): {problems!r}")
    # `verdict` is asserted only where the case declares one. The
    # added-extension case deliberately does not: see
    # BUNDLE_ACCOUNTING_KINDS above.
    if "verdict" in expected and report.get("verdict") != expected["verdict"]:
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

    declared_bundle = {
        (f["kind"], f["symbol"]) for f in expected.get("bundle_accounting_findings", [])
    }
    observed_bundle = {
        (f.get("kind"), module_stem(f.get("symbol") or ""))
        for f in (report.get("bundle_findings") or [])
        if f.get("kind") in BUNDLE_ACCOUNTING_KINDS
    }
    if observed_bundle != declared_bundle:
        errors.append(
            f"bundle accounting findings={sorted(observed_bundle)!r}, "
            f"expected {sorted(declared_bundle)!r}"
        )
    return errors


def other_bundle_findings(report: Dict[str, Any]) -> List[tuple]:
    """Bundle findings outside the accounting vocabulary, for the receipt."""
    return sorted(
        (f.get("kind"), f.get("symbol"))
        for f in (report.get("bundle_findings") or [])
        if f.get("kind") not in BUNDLE_ACCOUNTING_KINDS
    )


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


#: A minimal, genuinely loadable CPython extension. Plain C API rather than
#: pybind11: no build dependency beyond Python's own headers, and what
#: matters here is the module initializer, not the binding layer.
_EXTRA_MODULE_SOURCE = """\
#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *lab_extra_ping(PyObject *self, PyObject *args) {
  (void)self; (void)args;
  return PyLong_FromLong(42);
}

static PyMethodDef lab_extra_methods[] = {
    {"ping", lab_extra_ping, METH_NOARGS, "Return 42."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef lab_extra_module = {
    PyModuleDef_HEAD_INIT, "%(module)s", NULL, -1, lab_extra_methods,
    NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit_%(module)s(void) {
  return PyModule_Create(&lab_extra_module);
}
"""


def build_extension_module(module: str, work: Path, cc: str = "cc") -> Path:
    """Compile a real extension module exporting ``PyInit_<module>``.

    Renaming a byte copy of the existing extension does NOT produce a second
    module: the copy still exports ``PyInit__core``, so importing it fails
    with "dynamic module does not define module export function". A scenario
    built on such a copy can pass on archive filename discovery alone while
    the "added module" is not loadable at all -- which is not what it claims
    to prove (Codex review, PR #30).
    """
    work.mkdir(parents=True, exist_ok=True)
    source = work / f"{module}.c"
    source.write_text(_EXTRA_MODULE_SOURCE % {"module": module}, encoding="utf-8")
    include = sysconfig.get_paths().get("include")
    if not include or not Path(include).is_dir():
        raise ScenarioError(
            "Python development headers (Python.h) are required to build the "
            f"second extension module; {include!r} is not a directory"
        )
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    built = work / f"{module}{suffix}"
    run_command([cc, "-shared", "-fPIC", f"-I{include}", str(source), "-o", str(built)])
    return built


def assert_module_is_loadable(extension: Path, module: str) -> None:
    """Import the built module in a subprocess to prove it really loads.

    In a subprocess, not this interpreter: importing an extension cannot be
    undone, and a broken one can abort the process outright.
    """
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location({module!r}, {str(extension)!r});"
        "mod = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(mod);"
        "sys.exit(0 if mod.ping() == 42 else 1)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    if result.returncode != 0:
        raise ScenarioError(
            f"{extension.name} does not load as module {module!r}: "
            f"{(result.stderr or '').strip()}"
        )


def _record_line(member: str, payload: bytes) -> str:
    """One PEP 376 RECORD row: `path,sha256=<urlsafe-b64-nopad>,size`."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return f"{member},sha256={digest.decode('ascii').rstrip('=')},{len(payload)}"


def rewrite_record(archive_path: Path, added: Dict[str, bytes]) -> None:
    """Add `added` to the wheel's .dist-info/RECORD.

    A wheel is not just a zip: RECORD lists every installed file with its
    hash and size, and installers and repair tools (auditwheel, `pip
    install`) use it. Appending a member without a RECORD row leaves an
    archive that is not a valid installable wheel, so a green comparison
    would prove ZIP-member discovery rather than addition to a real package
    (Codex review, PR #30).

    Rewriting a zip member in place is not possible, so the archive is
    rebuilt with the updated RECORD.
    """
    with zipfile.ZipFile(archive_path) as source:
        names = source.namelist()
        record_names = [n for n in names if n.endswith(".dist-info/RECORD")]
        if not record_names:
            raise ScenarioError(f"{archive_path.name}: no .dist-info/RECORD to update")
        record_name = record_names[0]
        contents = {name: source.read(name) for name in names}

    try:
        record = contents[record_name].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScenarioError(f"{archive_path.name}: RECORD is not UTF-8: {exc}") from exc
    rows = [line for line in record.splitlines() if line.strip()]
    # RECORD's own row carries no hash or size and must stay last.
    own = [row for row in rows if row.startswith(f"{record_name},")]
    rows = [row for row in rows if not row.startswith(f"{record_name},")]
    for member, payload in sorted(added.items()):
        rows = [row for row in rows if not row.startswith(f"{member},")]
        rows.append(_record_line(member, payload))
    rows.extend(own or [f"{record_name},,"])
    contents[record_name] = ("\n".join(rows) + "\n").encode("utf-8")

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as target:
        for name in names:
            target.writestr(name, contents[name])


def assert_wheel_installs(wheel: Path, modules: List[str], work: Path) -> None:
    """Install the wheel and import each module, proving it is really valid.

    The end-to-end answer to "is this a package or just a zip": pip honours
    RECORD, so a wheel with a missing or wrong row fails here rather than
    silently comparing as if it were installable.
    """
    target = work / "install"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
         "--target", str(target), str(wheel)],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        raise ScenarioError(
            f"{wheel.name} does not install: {(install.stderr or '').strip()}"
        )
    probe = (
        "import importlib, sys;"
        f"sys.path.insert(0, {str(target)!r});"
        + ";".join(f"importlib.import_module('abicheck_lab_py.{m}')" for m in modules)
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    if result.returncode != 0:
        raise ScenarioError(
            f"{wheel.name} installed but {modules!r} did not import: "
            f"{(result.stderr or '').strip()}"
        )


def add_extension_module(wheel: Path, out_dir: Path, module: str, work: Path) -> Path:
    """Repack ``wheel`` with a second, genuinely loadable extension module.

    The added module is compiled for this interpreter with the matching
    ``PyInit_<module>`` initializer and is import-checked before it goes into
    the archive, so the package-level accounting this exercises is accounting
    for a real extension rather than for a renamed file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / wheel.name
    shutil.copy2(wheel, target)
    with zipfile.ZipFile(wheel) as archive:
        existing = [n for n in archive.namelist() if n.endswith((".so", ".pyd"))]
    if not existing:
        raise ScenarioError(f"{wheel.name}: no extension to sit alongside")
    package = str(Path(existing[0]).parent)

    built = build_extension_module(module, work)
    assert_module_is_loadable(built, module)

    member = f"{package}/{built.name}" if package not in ("", ".") else built.name
    payload = built.read_bytes()
    with zipfile.ZipFile(target, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
    rewrite_record(target, {member: payload})
    assert_wheel_installs(target, ["_core", module], work)
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
            new = add_extension_module(
                old, output / f"{case_id}-plus", case["added_module"],
                output / f"{case_id}-build",
            )
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
        summary[case_id] = {
            "result": case_errors or "ok",
            "verdict": report.get("verdict"),
            "bundle_verdict": report.get("bundle_verdict"),
            # Recorded, not asserted -- see BUNDLE_ACCOUNTING_KINDS.
            "other_bundle_findings": other_bundle_findings(report),
        }
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
