"""Unit tests for scripts/classify_canary_outcome.py (PR5 five-way canary
failure classification, design doc section 24)."""
from __future__ import annotations

import pytest

from classify_canary_outcome import ClassificationError, classify


def _signal(name, category, outcome):
    return {"name": name, "category": category, "outcome": outcome}


def test_all_success_classifies_success():
    result = classify(
        [
            _signal("checkout", "infrastructure", "success"),
            _signal("semantic canary", "semantic", "success"),
            _signal("action contract canary", "action_contract", "success"),
            _signal("build integration canary", "build_integration", "success"),
        ]
    )
    assert result == {"classification": "SUCCESS", "reasons": []}


def test_infrastructure_failure_wins_even_with_a_drift_failure_present():
    result = classify(
        [
            _signal("install castxml", "infrastructure", "failure"),
            _signal("semantic canary", "semantic", "failure"),
        ]
    )
    assert result["classification"] == "INFRASTRUCTURE_FAILED"
    assert any("install castxml" in reason for reason in result["reasons"])


def test_skipped_drift_signal_is_infrastructure_not_drift():
    # A downstream layer that never ran (because an earlier infra step
    # failed) must never be reported as a detector/contract/build
    # regression -- section 24's explicit requirement.
    result = classify(
        [
            _signal("checkout", "infrastructure", "failure"),
            _signal("semantic canary", "semantic", "skipped"),
        ]
    )
    assert result["classification"] == "INFRASTRUCTURE_FAILED"
    assert len(result["reasons"]) == 2


def test_semantic_drift_only():
    result = classify(
        [
            _signal("checkout", "infrastructure", "success"),
            _signal("semantic canary", "semantic", "failure"),
            _signal("action contract canary", "action_contract", "success"),
        ]
    )
    assert result["classification"] == "DETECTOR_DRIFT"
    assert result["reasons"] == ["semantic canary (semantic): failure"]


def test_action_contract_drift_only():
    result = classify(
        [
            _signal("checkout", "infrastructure", "success"),
            _signal("semantic canary", "semantic", "success"),
            _signal("check-target contract", "action_contract", "failure"),
        ]
    )
    assert result["classification"] == "ACTION_CONTRACT_DRIFT"


def test_build_integration_drift_only():
    result = classify(
        [
            _signal("checkout", "infrastructure", "success"),
            _signal("cmake profile compare", "build_integration", "failure"),
        ]
    )
    assert result["classification"] == "BUILD_INTEGRATION_DRIFT"


def test_priority_order_when_multiple_drift_categories_fail():
    result = classify(
        [
            _signal("semantic canary", "semantic", "failure"),
            _signal("action contract canary", "action_contract", "failure"),
            _signal("build integration canary", "build_integration", "failure"),
        ]
    )
    # Doc order: DETECTOR_DRIFT listed before ACTION_CONTRACT_DRIFT before
    # BUILD_INTEGRATION_DRIFT -- semantic wins the tie.
    assert result["classification"] == "DETECTOR_DRIFT"


def test_empty_signals_is_an_error():
    with pytest.raises(ClassificationError):
        classify([])


@pytest.mark.parametrize(
    "signal",
    [
        {"category": "infrastructure", "outcome": "success"},  # missing name
        {"name": "x", "outcome": "success"},  # missing category
        {"name": "x", "category": "not-a-real-category", "outcome": "success"},  # bad category
        {"name": "x", "category": "infrastructure"},  # missing outcome
        "not-a-dict",
    ],
)
def test_invalid_signal_shapes_are_rejected(signal):
    with pytest.raises(ClassificationError):
        classify([signal])
