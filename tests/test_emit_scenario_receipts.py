"""Unit tests for scripts/emit_scenario_receipts.py's status derivation --
the mapping from run_scenario.py's summary.json to the two
detection-correctness-scenarios-{castxml,clang} receipt statuses.
"""

from __future__ import annotations

from emit_scenario_receipts import derive_statuses


def _result(profile, passed):
    return {"profile": profile, "passed": passed}


def test_all_passed_per_profile_yields_passed():
    results = [
        _result("castxml", True),
        _result("castxml", True),
        _result("clang", True),
    ]
    statuses = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "passed"
    assert statuses["detection-correctness-scenarios-clang"] == "passed"


def test_one_failure_in_profile_yields_failed():
    results = [
        _result("castxml", True),
        _result("castxml", False),
        _result("clang", True),
    ]
    statuses = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "passed"


def test_profile_absent_from_results_yields_skipped():
    results = [_result("castxml", True)]
    statuses = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "passed"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"


def test_results_with_no_profile_field_ignored_for_both_capabilities():
    # A backward-compatible single-oracle scenario (no per-profile
    # `expected:` map) writes profile=None -- it must not count toward
    # either castxml or clang's own execution proof.
    results = [{"profile": None, "passed": True}]
    statuses = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "skipped"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"


def test_empty_results_yields_skipped_for_both():
    statuses = derive_statuses([])
    assert statuses["detection-correctness-scenarios-castxml"] == "skipped"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"
