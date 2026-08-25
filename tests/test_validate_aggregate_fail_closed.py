from validate_aggregate_fail_closed import validate_result


def test_missing_required_reports_are_fail_closed():
    aggregate = {"coverage": {"missing_required_targets": ["a", "b"]}}
    assert validate_result(1, aggregate, 2) == []
    assert validate_result(0, aggregate, 2)
    assert validate_result(1, aggregate, 3)
