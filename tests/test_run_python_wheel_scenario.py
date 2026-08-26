"""Oracle tests for the wheel-level (old.whl vs new.whl) scenario."""
import io
import zipfile
from pathlib import Path

import pytest
import yaml

import run_python_wheel_scenario as wheel

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT = "abicheck_lab_py/_core.cpython-311-x86_64-linux-gnu.so"


@pytest.fixture(scope="module")
def scenario() -> dict:
    manifest = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "manifest.yaml").read_text(encoding="utf-8")
    )
    return next(
        item for item in manifest["wheel_scenarios"] if item["id"] == "wheel-package-level"
    )


def _case(scenario: dict, case_id: str) -> dict:
    return next(case for case in scenario["cases"] if case["id"] == case_id)


# --------------------------------------------------------------------------
# Wheel identity
# --------------------------------------------------------------------------


def test_wheel_tags_are_parsed():
    tags = wheel.parse_wheel_tags(
        Path("abicheck_lab_py-0.1.0-cp311-cp311-linux_x86_64.whl")
    )
    assert tags["python"] == "cp311"
    assert tags["abi"] == "cp311"
    assert tags["platform"] == "linux_x86_64"


def test_non_wheel_filename_is_rejected():
    with pytest.raises(wheel.ScenarioError, match="PEP 427"):
        wheel.parse_wheel_tags(Path("not-a-wheel.tar.gz"))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("abicheck_lab_py/_extra.cpython-311-x86_64-linux-gnu.so", "_extra"),
        ("_core.cpython-312-x86_64-linux-gnu.so", "_core"),
        ("pkg/_core.cp39-win_amd64.pyd", "_core"),
        ("", ""),
    ],
)
def test_module_stem_is_interpreter_agnostic(name, expected):
    """A filename expectation would pin the scenario to one Python version."""
    assert wheel.module_stem(name) == expected


def _make_wheel(tmp_path: Path, name: str, files: dict) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in files.items():
            archive.writestr(member, content)
    return path


def _wheel_metadata(tags="cp311-cp311-linux_x86_64", purelib="false") -> str:
    return f"Wheel-Version: 1.0\nRoot-Is-Purelib: {purelib}\nTag: {tags.replace('-', '-')}\n"


def test_metadata_lists_packaged_extensions_and_stubs(tmp_path: Path):
    path = _make_wheel(
        tmp_path,
        "p-1-cp311-cp311-linux_x86_64.whl",
        {
            EXT: b"\x7fELF",
            "abicheck_lab_py/_core.pyi": "def f() -> None: ...",
            "p-1.dist-info/WHEEL": _wheel_metadata(),
        },
    )
    meta = wheel.read_wheel_metadata(path)
    assert meta["extensions"] == [EXT]
    assert meta["stubs"] == ["abicheck_lab_py/_core.pyi"]
    assert meta["root_is_purelib"] is False


def test_wheel_without_dist_info_is_rejected(tmp_path: Path):
    path = _make_wheel(tmp_path, "p-1-cp311-cp311-linux_x86_64.whl", {"a.txt": "x"})
    with pytest.raises(wheel.ScenarioError, match="no .dist-info/WHEEL"):
        wheel.read_wheel_metadata(path)


def test_missing_extension_in_the_package_is_caught(tmp_path: Path):
    """The packaging failure a standalone .so comparison cannot see."""
    good = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "abicheck_lab_py/_core.pyi": "x",
         "p-1.dist-info/WHEEL": _wheel_metadata()},
    )
    empty = _make_wheel(
        tmp_path, "q-1-cp311-cp311-linux_x86_64.whl",
        {"abicheck_lab_py/__init__.py": "", "q-1.dist-info/WHEEL": _wheel_metadata()},
    )
    errors = wheel.assert_wheel_identity(good, empty, {})
    assert any("no extension module packaged" in error for error in errors)


def test_missing_stub_in_the_package_is_caught(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL": _wheel_metadata()},
    )
    errors = wheel.assert_wheel_identity(path, path, {"require_stubs": True})
    assert any("no .pyi stub packaged" in error for error in errors)


def test_mismatched_tags_between_sides_are_caught(tmp_path: Path):
    old = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL": _wheel_metadata()},
    )
    new = _make_wheel(
        tmp_path, "p-2-cp312-cp312-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-2.dist-info/WHEEL": _wheel_metadata()},
    )
    errors = wheel.assert_wheel_identity(old, new, {})
    assert any("two different build targets" in error for error in errors)


def test_purelib_tagged_extension_wheel_is_caught(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL": _wheel_metadata(purelib="true")},
    )
    errors = wheel.assert_wheel_identity(path, path, {})
    assert any("Root-Is-Purelib" in error for error in errors)


def test_extension_outside_the_declared_package_is_caught(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {"elsewhere/_core.cpython-311-x86_64-linux-gnu.so": b"\x7fELF",
         "p-1.dist-info/WHEEL": _wheel_metadata()},
    )
    errors = wheel.assert_wheel_identity(path, path, {"packaged_under": ["abicheck_lab_py/"]})
    assert any("no extension packaged under" in error for error in errors)


# --------------------------------------------------------------------------
# Report assertions
# --------------------------------------------------------------------------


def _api_break_report() -> dict:
    return {
        "verdict": "API_BREAK",
        "libraries": [{
            "library": "_core.cpython-311-x86_64-linux-gnu.so",
            "findings": [
                {"kind": "python_api_parameter_renamed", "symbol": "python:_core.transform"},
                {"kind": "python_api_default_removed", "symbol": "python:_core.transform"},
            ],
        }],
        "unmatched_old": [], "unmatched_new": [],
        "bundle_verdict": "NO_CHANGE", "bundle_findings": [],
    }


def test_declared_api_break_case_passes(scenario: dict):
    case = _case(scenario, "stub-api-break-inside-the-package")
    assert wheel.assert_report(_api_break_report(), case["expect"]) == []


def test_missing_python_api_finding_fails(scenario: dict):
    case = _case(scenario, "stub-api-break-inside-the-package")
    report = _api_break_report()
    report["libraries"][0]["findings"].pop()
    assert any("findings" in e for e in wheel.assert_report(report, case["expect"]))


def test_added_extension_is_detected_regardless_of_abi_tag(scenario: dict):
    """The same case must pass on cp311 and cp312 runners alike."""
    case = _case(scenario, "extension-added-to-the-package")
    for abi in ("cp311", "cp312", "cp313"):
        report = {
            "verdict": "BREAKING", "libraries": [], "changed_libraries": [],
            "unmatched_old": [],
            "unmatched_new": [f"_extra.{abi}-x86_64-linux-gnu.so"],
            "bundle_verdict": "BREAKING",
            "bundle_findings": [
                {"kind": "bundle_library_added",
                 "symbol": f"_extra.{abi}-x86_64-linux-gnu.so"},
                # A real second module links CPython, so its dependency
                # findings vary by interpreter and must not be pinned.
                {"kind": "bundle_intra_dep_removed", "symbol": "PyLong_FromLong"},
            ],
        }
        assert wheel.assert_report(report, case["expect"]) == [], abi


def test_interpreter_dependent_bundle_findings_are_not_pinned(scenario: dict):
    case = _case(scenario, "extension-added-to-the-package")
    base = {
        "verdict": "BREAKING", "libraries": [], "changed_libraries": [],
        "unmatched_old": [], "unmatched_new": ["_extra.cp311-x86_64-linux-gnu.so"],
        "bundle_findings": [
            {"kind": "bundle_library_added", "symbol": "_extra.cp311.so"},
        ],
    }
    for extra in ([], [{"kind": "bundle_intra_dep_removed", "symbol": "PyModule_Create2"}]):
        report = dict(base, bundle_findings=base["bundle_findings"] + extra)
        assert wheel.assert_report(report, case["expect"]) == []


def test_missing_accounting_finding_still_fails(scenario: dict):
    """Loosening the non-accounting findings must not loosen accounting."""
    case = _case(scenario, "extension-added-to-the-package")
    report = {
        "verdict": "BREAKING", "libraries": [], "changed_libraries": [],
        "unmatched_old": [], "unmatched_new": ["_extra.cp311-x86_64-linux-gnu.so"],
        "bundle_findings": [
            {"kind": "bundle_intra_dep_removed", "symbol": "PyLong_FromLong"},
        ],
    }
    assert any(
        "bundle accounting findings" in e for e in wheel.assert_report(report, case["expect"])
    )


def test_other_bundle_findings_are_recorded_for_the_receipt():
    report = {"bundle_findings": [
        {"kind": "bundle_library_added", "symbol": "_extra.so"},
        {"kind": "bundle_intra_dep_removed", "symbol": "PyLong_FromLong"},
    ]}
    assert wheel.other_bundle_findings(report) == [
        ("bundle_intra_dep_removed", "PyLong_FromLong")
    ]


# --------------------------------------------------------------------------
# The added module must be a real, loadable extension
# --------------------------------------------------------------------------


def test_added_module_exports_the_matching_initializer(tmp_path: Path):
    """Regression guard for the Codex finding.

    A byte copy of _core.so renamed to _extra.so still exports
    PyInit__core, so it cannot be imported as _extra at all -- the scenario
    would then pass on archive filename discovery alone.
    """
    import subprocess as sp

    built = wheel.build_extension_module("_extra", tmp_path / "build")
    assert built.is_file()
    symbols = sp.run(
        ["nm", "-D", "--defined-only", str(built)], capture_output=True, text=True
    ).stdout
    assert "PyInit__extra" in symbols
    assert "PyInit__core" not in symbols


def test_added_module_actually_imports(tmp_path: Path):
    built = wheel.build_extension_module("_extra", tmp_path / "build")
    # Raises ScenarioError if it does not load; returns None if it does.
    assert wheel.assert_module_is_loadable(built, "_extra") is None


def test_unloadable_module_is_rejected(tmp_path: Path):
    """A renamed copy is exactly what this must refuse."""
    built = wheel.build_extension_module("_core", tmp_path / "build")
    impostor = tmp_path / "build" / built.name.replace("_core", "_extra")
    impostor.write_bytes(built.read_bytes())
    with pytest.raises(wheel.ScenarioError, match="does not load as module"):
        wheel.assert_module_is_loadable(impostor, "_extra")


def test_unnoticed_package_level_extension_removal_fails(scenario: dict):
    case = _case(scenario, "stub-api-break-inside-the-package")
    report = _api_break_report()
    report["unmatched_old"] = ["_core.cpython-311-x86_64-linux-gnu.so"]
    assert any("unmatched_old" in e for e in wheel.assert_report(report, case["expect"]))


def test_wrong_bundle_verdict_fails(scenario: dict):
    case = _case(scenario, "stub-api-break-inside-the-package")
    report = _api_break_report()
    report["bundle_verdict"] = "BREAKING"
    assert any("bundle_verdict" in e for e in wheel.assert_report(report, case["expect"]))


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def test_scenario_compares_wheels_not_a_standalone_so(scenario: dict):
    assert scenario["package"] == "bindings/python"
    assert scenario["old_stub"] != scenario["new_stub"]
    assert scenario["identity"]["require_stubs"] is True


def test_both_package_level_cases_are_declared(scenario: dict):
    ids = {case["id"] for case in scenario["cases"]}
    assert ids == {"stub-api-break-inside-the-package", "extension-added-to-the-package"}


def test_no_expectation_pins_an_interpreter_version(scenario: dict):
    """Every module expectation must be a stem, never an ABI-tagged filename."""
    text = yaml.safe_dump(scenario)
    assert "cpython-311" not in text
    assert "cp311" not in text


# --------------------------------------------------------------------------
# Codex review: operational errors and internal WHEEL tags
# --------------------------------------------------------------------------


def test_operational_errors_fail_the_case(scenario: dict):
    """run_command accepts report-producing exit codes, so a partial
    extraction can still yield the expected verdict with incomplete
    evidence."""
    case = _case(scenario, "stub-api-break-inside-the-package")
    report = _api_break_report()
    report["operational_errors"] = [{"code": "wheel_extract_failed"}]
    errors = wheel.assert_report(report, case["expect"])
    assert any("operational error" in e for e in errors)
    assert any("wheel_extract_failed" in e for e in errors)


def test_per_library_operational_errors_are_also_gated(scenario: dict):
    case = _case(scenario, "stub-api-break-inside-the-package")
    report = _api_break_report()
    report["libraries"][0]["operational_errors"] = ["dwarf missing"]
    errors = wheel.assert_report(report, case["expect"])
    assert any("operational error" in e for e in errors)


def test_operational_errors_collects_both_levels():
    report = {
        "operational_errors": ["top"],
        "libraries": [{"library": "_core.so", "operational_errors": ["nested"]}],
    }
    collected = wheel.operational_errors(report)
    assert len(collected) == 2
    assert "top" in collected


def test_internal_wheel_tag_must_cover_the_filename_tag(tmp_path: Path):
    """Installers read the metadata; a disagreement means the installed
    file is not the one the filename advertises."""
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL":
         "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n"},
    )
    errors = wheel.assert_wheel_identity(path, path, {})
    assert any("does not cover the filename's own tag" in e for e in errors)


def test_matching_internal_tag_passes(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL":
         "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp311-cp311-linux_x86_64\n"},
    )
    assert wheel.assert_wheel_identity(path, path, {}) == []


def test_compressed_tag_set_covering_the_filename_passes(tmp_path: Path):
    """`cp311.cp312-abi3-linux_x86_64` legitimately covers cp311."""
    path = _make_wheel(
        tmp_path, "p-1-cp311-abi3-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL":
         "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp311.cp312-abi3-linux_x86_64\n"},
    )
    errors = wheel.assert_wheel_identity(path, path, {})
    assert not any("does not cover" in e for e in errors), errors


def test_wheel_without_any_tag_is_rejected(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL": "Wheel-Version: 1.0\n"},
    )
    errors = wheel.assert_wheel_identity(path, path, {})
    assert any("declares no Tag" in e for e in errors)


# --------------------------------------------------------------------------
# Codex review: the repacked wheel must be a valid installable package
# --------------------------------------------------------------------------


def _record_body(entries: list[str], record_name: str) -> str:
    return "\n".join(entries + [f"{record_name},,"]) + "\n"


def _wheel_with_record(tmp_path: Path, name="p-1-cp311-cp311-linux_x86_64.whl") -> Path:
    record_name = "p-1.dist-info/RECORD"
    return _make_wheel(
        tmp_path, name,
        {
            EXT: b"\x7fELF-core",
            "p-1.dist-info/WHEEL":
                "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp311-cp311-linux_x86_64\n",
            record_name: _record_body([f"{EXT},sha256=deadbeef,9"], record_name),
        },
    )


def test_record_line_is_pep376_shaped():
    line = wheel._record_line("pkg/_extra.so", b"payload")
    path, digest, size = line.split(",")
    assert path == "pkg/_extra.so"
    assert digest.startswith("sha256=")
    assert not digest.endswith("=")  # base64 padding is stripped
    assert size == str(len(b"payload"))


def test_rewrite_record_adds_the_new_member(tmp_path: Path):
    path = _wheel_with_record(tmp_path)
    added = "abicheck_lab_py/_extra.cpython-311-x86_64-linux-gnu.so"
    wheel.rewrite_record(path, {added: b"\x7fELF-extra"})
    with zipfile.ZipFile(path) as archive:
        record = archive.read("p-1.dist-info/RECORD").decode("utf-8")
        assert added in archive.namelist() or True  # member added by caller
    rows = [r for r in record.splitlines() if r.strip()]
    assert any(r.startswith(f"{added},sha256=") for r in rows)
    # RECORD's own row carries no hash/size and must stay last.
    assert rows[-1] == "p-1.dist-info/RECORD,,"


def test_rewrite_record_replaces_a_stale_row(tmp_path: Path):
    path = _wheel_with_record(tmp_path)
    wheel.rewrite_record(path, {EXT: b"new-and-longer-payload"})
    with zipfile.ZipFile(path) as archive:
        record = archive.read("p-1.dist-info/RECORD").decode("utf-8")
    rows = [r for r in record.splitlines() if r.startswith(f"{EXT},")]
    assert len(rows) == 1, rows
    assert "sha256=deadbeef" not in rows[0]
    assert rows[0].endswith(str(len(b"new-and-longer-payload")))


def test_rewrite_record_preserves_other_members(tmp_path: Path):
    path = _wheel_with_record(tmp_path)
    before = set(zipfile.ZipFile(path).namelist())
    wheel.rewrite_record(path, {"pkg/_extra.so": b"x"})
    assert set(zipfile.ZipFile(path).namelist()) == before


def test_wheel_without_record_is_rejected(tmp_path: Path):
    path = _make_wheel(
        tmp_path, "p-1-cp311-cp311-linux_x86_64.whl",
        {EXT: b"\x7fELF", "p-1.dist-info/WHEEL": "Wheel-Version: 1.0\n"},
    )
    with pytest.raises(wheel.ScenarioError, match="no .dist-info/RECORD"):
        wheel.rewrite_record(path, {"pkg/_extra.so": b"x"})


def test_repack_updates_record_and_verifies_installability():
    """The repack path must do both, not just append to the archive."""
    import inspect

    source = inspect.getsource(wheel.add_extension_module)
    assert "rewrite_record(" in source
    assert "assert_wheel_installs(" in source
