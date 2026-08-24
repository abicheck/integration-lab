from validate_source_depth import validate


def test_complete_source_assurance_passes():
    report = {"analysis_assurance": {
        "status": "complete", "requested_depth": "source", "effective_depth": "source"
    }}
    assert validate(report) == []


def test_shallow_or_incomplete_report_fails():
    report = {"analysis_assurance": {
        "status": "incomplete", "requested_depth": "source", "effective_depth": "build"
    }}
    errors = validate(report)
    assert any("effective_depth" in error for error in errors)
    assert any("status" in error for error in errors)


def test_operational_errors_fail_even_at_source_depth():
    report = {"level": {"depth": "source"}, "operational_errors": [{"kind": "evidence"}]}
    assert any("operational" in error for error in validate(report))
