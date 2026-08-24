from run_runtime_floor_scenario import _oracle


def test_runtime_floor_oracle_checks_severity_and_verdict():
    report = {"verdict": "BREAKING", "changes": [{
        "kind": "runtime_floor_raised", "symbol": "liblabrt.so:LABRT", "severity": "breaking"
    }]}
    assert _oracle(report, {"verdict": "BREAKING", "floor_severity": "breaking"}) == []
    assert _oracle(report, {"verdict": "BREAKING", "floor_severity": "compatible"})
