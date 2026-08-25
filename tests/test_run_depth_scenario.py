from run_depth_scenario import _assert_report


def test_depth_report_oracle_requires_exact_finding():
    expected = {"verdict": "API_BREAK", "findings": [
        {"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}
    ]}
    good = {"verdict": "API_BREAK", "changes": [
        {"kind": "public_macro_value_changed", "symbol": "LAB_MAX_ITEMS"}
    ]}
    assert _assert_report(good, expected) == []
    bad = {"verdict": "API_BREAK", "changes": []}
    assert any("findings" in error for error in _assert_report(bad, expected))
