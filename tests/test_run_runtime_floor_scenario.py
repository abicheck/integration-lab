from run_runtime_floor_scenario import _oracle


def test_runtime_floor_oracle_checks_severity_and_verdict():
    report = {"verdict": "BREAKING", "changes": [
        {"kind": "symbol_version_required_added", "symbol": "LABRT_2.0", "severity": "breaking"},
        {"kind": "symbol_version_required_removed", "symbol": "LABRT_1.0", "severity": "compatible"},
        {"kind": "runtime_floor_raised", "symbol": "liblabrt.so:LABRT", "severity": "breaking"},
        {"kind": "imported_symbol_added", "symbol": "labrt_new_api", "severity": "risk"},
        {"kind": "imported_symbol_removed", "symbol": "labrt_api", "severity": "compatible"},
    ]}
    assert _oracle(report, {"verdict": "BREAKING", "floor_severity": "breaking"}) == []
    assert _oracle(report, {"verdict": "BREAKING", "floor_severity": "compatible"})

    report["changes"].append({"kind": "unexpected", "symbol": "x", "severity": "risk"})
    assert any("expected exactly" in error for error in _oracle(
        report, {"verdict": "BREAKING", "floor_severity": "breaking"}
    ))
