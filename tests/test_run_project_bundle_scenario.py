from run_project_bundle_scenario import _oracle


def test_bundle_oracle_attributes_provider_and_consumer():
    expected = {"verdict": "BREAKING", "bundle_finding": {
        "kind": "bundle_intra_dep_signature_changed", "symbol": "core_scale",
        "provider_library": "libcore.so", "consumer_library": "libmath.so",
    }}
    report = {"verdict": "BREAKING", "bundle_findings": [{**expected["bundle_finding"]}],
              "libraries": [{"library": "libcore.so", "verdict": "BREAKING"},
                            {"library": "libmath.so", "verdict": "NO_CHANGE"}]}
    assert _oracle(report, expected) == []
