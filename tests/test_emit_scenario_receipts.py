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
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "passed"
    assert statuses["detection-correctness-scenarios-clang"] == "passed"
    assert malformed == []


def test_one_failure_in_profile_yields_failed():
    results = [
        _result("castxml", True),
        _result("castxml", False),
        _result("clang", True),
    ]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "passed"
    assert malformed == []


def test_profile_absent_from_results_yields_skipped():
    results = [_result("castxml", True)]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "passed"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"
    assert malformed == []


def test_results_with_no_profile_field_ignored_for_both_capabilities():
    # A backward-compatible single-oracle scenario (no per-profile
    # `expected:` map) writes profile=None -- it must not count toward
    # either castxml or clang's own execution proof.
    results = [{"profile": None, "passed": True}]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "skipped"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"
    assert malformed == []


def test_empty_results_yields_skipped_for_both():
    statuses, malformed = derive_statuses([])
    assert statuses["detection-correctness-scenarios-castxml"] == "skipped"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"
    assert malformed == []


# --- Malformed-data handling (CodeRabbit review, fresh evidence) ---


def test_string_passed_value_is_malformed_not_truthy_passed():
    # "false" (a JSON string) is truthy in Python -- the naive
    # `all(r.get("passed") ...)` check this replaced would have emitted a
    # PASSED receipt for a scenario that actually reported failure.
    results = [{"profile": "castxml", "passed": "false"}]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "failed"
    assert len(malformed) == 1
    assert "non-boolean" in malformed[0]


def test_non_object_entry_is_malformed_not_a_crash():
    # A scalar entry has no .get() at all -- the naive per-profile filter
    # this replaced would have raised an unhandled AttributeError before
    # any receipt was written.
    results = [{"profile": "castxml", "passed": True}, "bad"]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "failed"
    assert len(malformed) == 1
    assert "not a JSON object" in malformed[0]


def test_valid_false_boolean_is_not_malformed():
    # A real JSON `false` is a legitimate, well-formed failure -- must not
    # be flagged as malformed data (only non-boolean/non-object shapes are).
    results = [_result("castxml", False)]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "skipped"
    assert malformed == []


def test_malformed_entry_for_one_profile_fails_both_capabilities():
    # A malformed entry can't be safely attributed to only the profile it
    # superficially names -- corruption anywhere in summary.json makes the
    # whole file untrustworthy, so both capabilities fail, not just the
    # one the bad entry happens to claim.
    results = [
        _result("castxml", "not-a-bool"),
        _result("clang", True),
    ]
    statuses, malformed = derive_statuses(results)
    assert statuses["detection-correctness-scenarios-castxml"] == "failed"
    assert statuses["detection-correctness-scenarios-clang"] == "failed"
    assert len(malformed) == 1
