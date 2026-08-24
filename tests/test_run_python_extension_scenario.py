from run_python_extension_scenario import _oracle


def test_python_oracle_requires_identical_binary_and_exact_findings():
    expected = {"binary_identical": True, "verdict": "API_BREAK", "findings": [
        {"kind": "python_api_parameter_renamed", "symbol": "python:_core.transform"}
    ]}
    report = {"verdict": "API_BREAK", "changes": [
        {"kind": "python_api_parameter_renamed", "symbol": "python:_core.transform"}
    ]}
    assert _oracle(report, expected, ["same", "same"]) == []
    assert any("byte-identical" in error for error in _oracle(report, expected, ["a", "b"]))
