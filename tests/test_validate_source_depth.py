from validate_source_depth import validate


def test_complete_source_assurance_passes():
    report = {"analysis_assurance": {
        "status": "complete", "requested_depth": "source", "effective_depth": "source",
        "depth_satisfied": True,
    }}
    assert validate(report) == []


def test_shallow_or_incomplete_report_fails():
    report = {"analysis_assurance": {
        "status": "incomplete", "requested_depth": "source", "effective_depth": "build",
        "depth_satisfied": False,
    }}
    errors = validate(report)
    assert any("effective_depth" in error for error in errors)
    assert any("depth_satisfied" in error for error in errors)


def test_operational_errors_fail_even_at_source_depth():
    report = {"level": {"depth": "source"}, "operational_errors": [{"kind": "evidence"}]}
    assert any("operational" in error for error in validate(report))


def test_level_only_report_does_not_prove_requested_depth():
    errors = validate({"level": {"depth": "source"}})
    assert any("requested_depth=None" in error for error in errors)


def test_partial_orthogonal_assurance_passes_when_source_depth_is_satisfied():
    report = {"analysis_assurance": {
        "status": "partial", "requested_depth": "source", "effective_depth": "source",
        "depth_satisfied": True, "notes": ["historical DWARF is asymmetric"],
    }}
    assert validate(report) == []
