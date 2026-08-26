"""Oracle tests for the producer-compiler attribution scenario."""
from pathlib import Path

import pytest
import yaml

import run_producer_compiler_scenario as scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
GCC = "linux-x86_64-gcc14-cxx17-cmake-ninja"
CLANG = "linux-x86_64-clang18-cxx17-cmake-ninja"


def test_breaking_producer_is_affected_and_clean_one_is_not():
    result = scenario.classify_profiles({GCC: "BREAKING", CLANG: "NO_CHANGE"})
    assert result == {"affected_profiles": [GCC], "unaffected_profiles": [CLANG]}


def test_compatible_counts_as_unaffected():
    result = scenario.classify_profiles({GCC: "COMPATIBLE", CLANG: "NO_CHANGE"})
    assert result["affected_profiles"] == []


@pytest.mark.parametrize("verdict", ["API_BREAK", "COMPATIBLE_WITH_RISK", "BREAKING"])
def test_anything_short_of_clean_counts_as_affected(verdict):
    result = scenario.classify_profiles({GCC: verdict})
    assert result["affected_profiles"] == [GCC]


def test_classification_is_total():
    """Every profile lands in exactly one list -- none can silently vanish."""
    verdicts = {GCC: "BREAKING", CLANG: "NO_CHANGE", "third": "SOMETHING_NEW"}
    result = scenario.classify_profiles(verdicts)
    assert sorted(result["affected_profiles"] + result["unaffected_profiles"]) == sorted(
        verdicts
    )


def test_expectation_match_passes():
    actual = {"affected_profiles": [GCC], "unaffected_profiles": [CLANG]}
    assert scenario.assert_expectation(actual, actual) == []


def test_attribution_loss_is_reported():
    """Both producers breaking means the producer axis was not honoured."""
    actual = {"affected_profiles": sorted([GCC, CLANG]), "unaffected_profiles": []}
    expected = {"affected_profiles": [GCC], "unaffected_profiles": [CLANG]}
    errors = scenario.assert_expectation(actual, expected)
    assert any("affected_profiles" in error for error in errors)
    assert any("unaffected_profiles" in error for error in errors)


def test_expectation_naming_only_one_side_is_rejected():
    """A scenario with no unaffected profile cannot detect attribution loss."""
    actual = {"affected_profiles": [GCC, CLANG], "unaffected_profiles": []}
    errors = scenario.assert_expectation(actual, actual)
    assert any("at least one affected AND one unaffected" in error for error in errors)


def test_missing_producer_compiler_fails_closed(monkeypatch):
    """Never skip: a missing producer makes the aggregate vacuous."""
    monkeypatch.setattr(scenario.shutil, "which", lambda tool: None)
    with pytest.raises(scenario.ScenarioError, match="not on PATH"):
        scenario._resolve_compiler({"cxx": "g++-14"}, GCC)


def test_producer_without_a_compiler_declaration_fails():
    with pytest.raises(scenario.ScenarioError, match="no cxx executable"):
        scenario._resolve_compiler({}, GCC)


def test_single_producer_scenario_is_rejected(tmp_path):
    manifest = tmp_path / "scenarios" / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump(
            {
                "producer_scenarios": [
                    {"id": "solo", "fixture": "fixtures/x", "producers": {GCC: {"cxx": "g++"}}}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(scenario.ScenarioError, match="at least two producer"):
        scenario.run(manifest, "solo", tmp_path / "out")


def test_unknown_scenario_id_fails(tmp_path):
    manifest = tmp_path / "scenarios" / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(yaml.safe_dump({"producer_scenarios": []}), encoding="utf-8")
    with pytest.raises(scenario.ScenarioError, match="no producer scenario"):
        scenario.run(manifest, "missing", tmp_path / "out")


# --------------------------------------------------------------------------
# The declared scenario and its fixture must actually agree with each other
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def declared() -> dict:
    manifest = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "manifest.yaml").read_text(encoding="utf-8")
    )
    return next(
        item
        for item in manifest["producer_scenarios"]
        if item["id"] == "producer-compiler-word-width"
    )


def test_declared_producers_are_real_profiles(declared: dict) -> None:
    profiles = yaml.safe_load((REPO_ROOT / "ci" / "profiles.yaml").read_text(encoding="utf-8"))
    known = {entry["id"] for entry in profiles["profiles"]}
    assert set(declared["producers"]) <= known


def test_declared_producers_differ_only_in_compiler(declared: dict) -> None:
    """The point of the scenario: vary the producer, not the build system."""
    profiles = {
        entry["id"]: entry
        for entry in yaml.safe_load(
            (REPO_ROOT / "ci" / "profiles.yaml").read_text(encoding="utf-8")
        )["profiles"]
    }
    entries = [profiles[pid] for pid in declared["producers"]]
    assert len({entry["backend"] for entry in entries}) == 1
    assert len({entry["generator"] for entry in entries}) == 1
    assert len({entry["compiler"]["family"] for entry in entries}) == len(entries)


def test_expectation_covers_every_declared_producer(declared: dict) -> None:
    expect = declared["expect"]
    accounted = set(expect["affected_profiles"]) | set(expect["unaffected_profiles"])
    assert accounted == set(declared["producers"])


def test_fixture_changes_only_the_non_clang_branch(declared: dict) -> None:
    """If the Clang branch moved too, the scenario would prove nothing."""
    fixture = REPO_ROOT / declared["fixture"]
    v1 = (fixture / "v1" / "api.h").read_text(encoding="utf-8").splitlines()
    v2 = (fixture / "v2" / "api.h").read_text(encoding="utf-8").splitlines()
    differing = [
        (a, b) for a, b in zip(v1, v2) if a != b
    ]
    assert len(v1) == len(v2), "fixture sides must stay line-aligned"
    assert differing, "fixture sides are identical; nothing would be detected"
    for old, new in differing:
        assert "__clang__" not in old and "__clang__" not in new
        assert "long" in new and "int" in old
    # The Clang arm is byte-identical on both sides.
    assert "using client_word = long;" in "\n".join(v1)
    assert "using client_word = long;" in "\n".join(v2)


def test_fixture_sources_are_identical(declared: dict) -> None:
    fixture = REPO_ROOT / declared["fixture"]
    assert (fixture / "v1" / "api.cc").read_text(encoding="utf-8") == (
        fixture / "v2" / "api.cc"
    ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Codex review: assert ABICheck's own findings, and scope the claim honestly
# --------------------------------------------------------------------------


def test_per_producer_findings_are_asserted_not_just_verdicts():
    """A verdict alone would pass on the right answer for the wrong reason."""
    expected = {
        "verdict": "BREAKING",
        "findings": [
            {"kind": "typedef_base_changed", "symbol": "client_word"},
            {"kind": "func_removed", "symbol": "_Z11lab_consumei"},
            {"kind": "func_added", "symbol": "_Z11lab_consumex"},
        ],
    }
    exact = {"verdict": "BREAKING", "changes": expected["findings"]}
    assert scenario.assert_producer_findings(exact, expected, GCC) == []

    right_verdict_wrong_reason = {
        "verdict": "BREAKING",
        "changes": [{"kind": "func_removed", "symbol": "something_else"}],
    }
    assert scenario.assert_producer_findings(right_verdict_wrong_reason, expected, GCC)


def test_clang_side_must_report_nothing():
    expected = {"verdict": "NO_CHANGE", "findings": []}
    assert scenario.assert_producer_findings({"verdict": "NO_CHANGE", "changes": []},
                                             expected, CLANG) == []
    stray = {"verdict": "NO_CHANGE", "changes": [{"kind": "func_added", "symbol": "x"}]}
    assert scenario.assert_producer_findings(stray, expected, CLANG)


def test_declaration_covers_every_producer_with_findings(declared: dict):
    per_producer = declared["expect"]["producers"]
    assert set(per_producer) == set(declared["producers"])
    for profile_id, expectation in per_producer.items():
        assert "verdict" in expectation
        assert "findings" in expectation


def test_the_breaking_producer_declares_the_mangled_symbol_pair(declared: dict):
    """The width change must be asserted through the symbols it produces."""
    gcc = declared["expect"]["producers"][GCC]
    symbols = {f["symbol"] for f in gcc["findings"]}
    assert "_Z11lab_consumei" in symbols
    assert "_Z11lab_consumex" in symbols


def test_project_path_attribution_is_declared_as_a_gap():
    """This scenario must not be read as proving per-profile routing."""
    manifest = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "manifest.yaml").read_text(encoding="utf-8")
    )
    gaps = {gap["id"] for gap in manifest["expected_gaps"]}
    assert "producer-attribution-through-project-path" in gaps
