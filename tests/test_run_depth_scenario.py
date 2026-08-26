"""Oracle tests for the deterministic L2-green/L4-red depth scenario."""
import run_depth_scenario
from run_depth_scenario import _assert_report


def test_depth_report_oracle_requires_exact_finding():
    expected = {
        "verdict": "API_BREAK",
        "effective_depth": "source",
        "findings": [{"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}],
    }
    assurance = {
        "status": "complete",
        "requested_depth": "source",
        "effective_depth": "source",
        "depth_satisfied": True,
    }
    good = {
        "verdict": "API_BREAK",
        "changes": [{"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}],
        "analysis_assurance": assurance,
    }
    assert _assert_report(good, expected, "source") == []
    bad = {"verdict": "API_BREAK", "changes": [], "analysis_assurance": assurance}
    assert any("findings" in error for error in _assert_report(bad, expected, "source"))


def _report(**overrides) -> dict:
    report = {
        "verdict": "API_BREAK",
        "changes": [{"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}],
        "operational_errors": [],
        "analysis_assurance": {
            "status": "complete",
            "requested_depth": "source",
            "effective_depth": "source",
            "depth_satisfied": True,
        },
    }
    report.update(overrides)
    return report


EXPECTED = {
    "verdict": "API_BREAK",
    "effective_depth": "source",
    "findings": [{"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}],
}


def test_matching_verdict_and_complete_assurance_passes():
    assert run_depth_scenario._assert_report(_report(), EXPECTED, "source") == []


def test_silent_l4_to_l2_fallback_fails_despite_the_right_verdict():
    """The whole point: the verdict can be right and the evidence wrong."""
    degraded = _report(
        analysis_assurance={
            "status": "partial",
            "requested_depth": "source",
            "effective_depth": "headers",
            "depth_satisfied": False,
            "notes": ["source graph unavailable"],
        }
    )
    errors = run_depth_scenario._assert_report(degraded, EXPECTED, "source")
    assert errors
    assert any("effective_depth" in error for error in errors)
    assert any("depth_satisfied" in error for error in errors)


def test_partial_assurance_alone_fails_the_scenario():
    partial = _report(
        analysis_assurance={
            "status": "partial",
            "requested_depth": "source",
            "effective_depth": "source",
            "depth_satisfied": True,
            "notes": ["two TUs unaccounted"],
        }
    )
    errors = run_depth_scenario._assert_report(partial, EXPECTED, "source")
    assert any("status='partial'" in error for error in errors)
    # The note explaining the gap is carried into the failure message.
    assert any("two TUs unaccounted" in error for error in errors)


def test_missing_assurance_block_fails():
    bare = _report()
    del bare["analysis_assurance"]
    errors = run_depth_scenario._assert_report(bare, EXPECTED, "source")
    assert any("analysis_assurance is missing" in error for error in errors)


def test_operational_errors_are_reported_with_their_content():
    noisy = _report(operational_errors=[{"code": "castxml_failed"}])
    errors = run_depth_scenario._assert_report(noisy, EXPECTED, "source")
    assert any("castxml_failed" in error for error in errors)


def test_source_compare_requests_complete_analysis(monkeypatch, tmp_path):
    """The source-depth compare must carry --require-complete-analysis."""
    import inspect

    source = inspect.getsource(run_depth_scenario.run)
    assert "--require-complete-analysis" in source
    assert 'if depth == "source"' in source
