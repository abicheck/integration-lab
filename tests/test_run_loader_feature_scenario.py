"""Oracle tests for the emitted loader/runtime feature drift scenario."""
from pathlib import Path

import pytest
import yaml

import run_loader_feature_scenario as lf

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cases() -> dict:
    manifest = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "manifest.yaml").read_text(encoding="utf-8")
    )
    return {case["id"]: case for case in manifest["loader_scenarios"]}


# --------------------------------------------------------------------------
# The declaration must stay about emitted features, not producer versions
# --------------------------------------------------------------------------


def test_declaration_models_emitted_features_not_version_floors(cases: dict) -> None:
    assert lf.assert_no_version_floor_vocabulary(list(cases.values())) == []


@pytest.mark.parametrize(
    "declaration",
    [
        [{"id": "x", "min_linker_version": "2.38"}],
        [{"id": "x", "note": "requires binutils 2.38"}],
        [{"id": "x", "expect": {"toolchain_version": "2.38"}}],
    ],
)
def test_version_floor_vocabulary_is_rejected(declaration) -> None:
    """binutils is a producer tool; a loader never sees which one built this."""
    assert lf.assert_no_version_floor_vocabulary(declaration)


# --------------------------------------------------------------------------
# Loader-feature classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "dt_relr_introduced", "dt_relr_removed", "rpath_type_changed",
        "hash_style_removed", "soname_changed", "soname_bump_unnecessary",
        "symbol_version_defined_added", "symbol_version_node_removed",
        "cet_protection_weakened", "cet_protection_improved",
        "static_tls_introduced",
    ],
)
def test_loader_feature_kinds_are_recognised(kind: str) -> None:
    assert lf.is_loader_feature(kind)


@pytest.mark.parametrize(
    "kind",
    ["imported_symbol_removed", "needed_removed", "func_added", "", None],
)
def test_incidental_kinds_are_not_loader_features(kind) -> None:
    """Codegen flags move symbols around; that churn is not this subject."""
    assert not lf.is_loader_feature(kind)


# --------------------------------------------------------------------------
# Case assertion
# --------------------------------------------------------------------------


def _report(changes, verdict="COMPATIBLE_WITH_RISK") -> dict:
    return {"verdict": verdict, "operational_errors": [], "changes": changes}


def test_declared_case_passes(cases: dict) -> None:
    case = cases["dt-relr-introduced"]
    report = _report([{"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "risk"}])
    assert lf.assert_case(report, case) == []


def test_missing_loader_finding_fails(cases: dict) -> None:
    assert any(
        "missing loader findings" in error
        for error in lf.assert_case(_report([]), cases["dt-relr-introduced"])
    )


def test_undeclared_loader_finding_fails(cases: dict) -> None:
    """A silently gained loader signal must not pass."""
    report = _report([
        {"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "risk"},
        {"kind": "hash_style_removed", "symbol": ".hash", "severity": "risk"},
    ])
    assert any(
        "undeclared loader findings" in error
        for error in lf.assert_case(report, cases["dt-relr-introduced"])
    )


def test_incidental_non_loader_churn_is_tolerated(cases: dict) -> None:
    """Which GLIBC_* node __tls_get_addr lives in varies by glibc."""
    report = _report([
        {"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "risk"},
        {"kind": "imported_symbol_removed", "symbol": "__tls_get_addr", "severity": "compatible"},
    ])
    assert lf.assert_case(report, cases["dt-relr-introduced"]) == []


def test_wrong_severity_fails(cases: dict) -> None:
    report = _report([
        {"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "compatible"}
    ])
    assert lf.assert_case(report, cases["dt-relr-introduced"])


def test_wrong_verdict_fails(cases: dict) -> None:
    report = _report(
        [{"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "risk"}],
        verdict="BREAKING",
    )
    assert any("verdict" in error for error in lf.assert_case(report, cases["dt-relr-introduced"]))


def test_operational_errors_fail_the_case(cases: dict) -> None:
    report = _report([{"kind": "dt_relr_introduced", "symbol": "DT_RELR", "severity": "risk"}])
    report["operational_errors"] = [{"code": "readelf_failed"}]
    assert any("operational" in error for error in lf.assert_case(report, cases["dt-relr-introduced"]))


# --------------------------------------------------------------------------
# Coverage and shape of the declared set
# --------------------------------------------------------------------------


def test_every_class_the_roadmap_names_is_covered(cases: dict) -> None:
    kinds = {
        finding["kind"]
        for case in cases.values()
        for finding in case["expect"]["findings"]
    }
    for required in (
        "dt_relr_introduced", "dt_relr_removed", "rpath_type_changed",
        "hash_style_removed", "soname_changed", "symbol_version_defined_added",
        "symbol_version_node_removed", "cet_protection_weakened",
        "static_tls_introduced",
    ):
        assert required in kinds, required


def test_every_case_declares_what_both_sides_must_emit(cases: dict) -> None:
    """Without this, an ignored flag yields two identical binaries."""
    for case_id, case in cases.items():
        for side in ("old", "new"):
            emits = case[side]["emits"]
            assert emits.get("present") or emits.get("absent"), f"{case_id}/{side}"


def test_every_case_declares_a_finding(cases: dict) -> None:
    for case_id, case in cases.items():
        assert case["expect"]["findings"], case_id


def test_reversible_classes_declare_both_directions(cases: dict) -> None:
    """A scanner reporting one severity in both directions must fail."""
    for forward, backward in (
        ("dt-relr-introduced", "dt-relr-removed"),
        ("cet-protection-weakened", "cet-protection-improved"),
        ("symbol-version-node-added", "symbol-version-node-removed"),
        ("runpath-to-rpath", "rpath-to-runpath"),
    ):
        assert forward in cases and backward in cases
        assert cases[forward]["old"]["link"] == cases[backward]["new"]["link"]
        assert cases[forward]["new"]["link"] == cases[backward]["old"]["link"]


def test_both_sides_build_the_same_source(cases: dict) -> None:
    """Only linker/codegen flags may differ, never the C source."""
    for case_id, case in cases.items():
        assert case["fixture"] == "fixtures/loader_features", case_id
        assert case["old"]["link"] != case["new"]["link"], case_id
