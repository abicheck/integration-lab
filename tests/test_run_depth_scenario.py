"""Oracle tests for the deterministic L2-green/L4-red depth scenario.

The expectations here were corrected against real `abicheck compare` output
(see run_depth_scenario._assert_report's docstring): the compare report's
own requested_depth/depth_satisfied are always None, and its effective_depth
reads `source` for both stages, so neither can detect a depth fallback.
fact_set_comparability/l3_context_status can, and do.
"""
from pathlib import Path

import pytest
import yaml

import run_depth_scenario
from run_depth_scenario import _assert_report

REPO_ROOT = Path(__file__).resolve().parent.parent

SOURCE_ASSURANCE = {
    "status": "complete",
    "fact_set_comparability": "comparable",
    "l3_context_status": "clean",
    # Always None on a snapshot-to-snapshot compare; present here so a test
    # that starts asserting them would fail loudly rather than by accident.
    "requested_depth": None,
    "depth_satisfied": None,
    "effective_depth": "source",
    "notes": [],
}


def _expected() -> dict:
    manifest = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "manifest.yaml").read_text(encoding="utf-8")
    )
    scenario = next(
        item
        for item in manifest["depth_scenarios"]
        if item["id"] == "l2-green-l4-macro-break"
    )
    return scenario["expect"]


def _source_report(**overrides) -> dict:
    report = {
        "verdict": "API_BREAK",
        "changes": [{"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}],
        "operational_errors": [],
        "analysis_assurance": dict(SOURCE_ASSURANCE),
    }
    report.update(overrides)
    return report


def test_depth_report_oracle_requires_exact_finding():
    expected = _expected()["source"]
    assert _assert_report(_source_report(), expected) == []
    bad = _source_report(changes=[])
    assert any("findings" in error for error in _assert_report(bad, expected))


def test_wrong_verdict_is_reported():
    expected = _expected()["source"]
    errors = _assert_report(_source_report(verdict="NO_CHANGE"), expected)
    assert any("verdict" in error for error in errors)


def test_silent_l4_to_l2_fallback_fails_despite_the_right_verdict():
    """The whole point: the verdict can be right and the evidence wrong."""
    degraded = _source_report()
    degraded["analysis_assurance"]["l3_context_status"] = "not_evaluated"
    degraded["analysis_assurance"]["fact_set_comparability"] = "not_applicable"
    errors = _assert_report(degraded, _expected()["source"])
    assert any("l3_context_status" in error for error in errors)
    assert any("fact_set_comparability" in error for error in errors)


def test_partial_assurance_fails_the_source_stage_with_its_notes():
    partial = _source_report()
    partial["analysis_assurance"]["status"] = "partial"
    partial["analysis_assurance"]["notes"] = ["two TUs unaccounted"]
    errors = _assert_report(partial, _expected()["source"])
    assert any("status='partial'" in error for error in errors)
    assert any("two TUs unaccounted" in error for error in errors)


def test_headers_stage_tolerates_its_orthogonal_partial_assurance():
    """`partial` there comes from public-surface scoping, not from depth."""
    expected = _expected()["headers"]
    report = {
        "verdict": "NO_CHANGE",
        "changes": [],
        "operational_errors": [],
        "analysis_assurance": {
            "status": "partial",
            "fact_set_comparability": "not_applicable",
            "l3_context_status": "not_evaluated",
            "notes": ["scope_resolved is False"],
        },
    }
    assert _assert_report(report, expected) == []


def test_headers_stage_still_fails_if_that_channel_changes():
    expected = _expected()["headers"]
    report = {
        "verdict": "NO_CHANGE",
        "changes": [],
        "operational_errors": [],
        "analysis_assurance": {
            "status": "failed",
            "fact_set_comparability": "not_applicable",
            "l3_context_status": "not_evaluated",
        },
    }
    assert any("status='failed'" in error for error in _assert_report(report, expected))


def test_missing_assurance_block_fails():
    bare = _source_report()
    del bare["analysis_assurance"]
    errors = _assert_report(bare, _expected()["source"])
    assert any("analysis_assurance is missing" in error for error in errors)


def test_operational_errors_are_reported_with_their_content():
    noisy = _source_report(operational_errors=[{"code": "castxml_failed"}])
    errors = _assert_report(noisy, _expected()["source"])
    assert any("castxml_failed" in error for error in errors)


# --------------------------------------------------------------------------
# The declared contract itself
# --------------------------------------------------------------------------


def test_both_stages_declare_compare_assurance():
    expected = _expected()
    for stage in ("headers", "source"):
        assert expected[stage]["compare_assurance"], stage


def test_source_stage_demands_complete_assurance():
    assert _expected()["source"]["compare_assurance"]["status"] == "complete"


def test_source_compare_requests_complete_analysis():
    """--require-complete-analysis must be on the source-depth compare."""
    import inspect

    source = inspect.getsource(run_depth_scenario.run)
    assert "--require-complete-analysis" in source
    assert 'if depth == "source"' in source


def test_dump_side_gates_both_requested_and_effective_depth():
    """The dump provenance is where a depth fallback actually shows."""
    import inspect

    source = inspect.getsource(run_depth_scenario.run)
    assert 'provenance.get("requested_depth") != depth' in source
    assert 'provenance.get("effective_depth") != expected_effective' in source
