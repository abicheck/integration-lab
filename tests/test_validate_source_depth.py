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


def _source_report(**assurance) -> dict:
    block = {
        "status": "complete",
        "requested_depth": "source",
        "effective_depth": "source",
        "depth_satisfied": True,
    }
    block.update(assurance)
    return {"analysis_assurance": block, "operational_errors": []}


def test_partial_assurance_passes_by_default():
    """Default stays lenient: `partial` can be an orthogonal channel."""
    assert validate(_source_report(status="partial")) == []


def test_partial_assurance_fails_under_require_complete():
    errors = validate(_source_report(status="partial"), require_complete=True)
    assert any("status='partial'" in error for error in errors)


def test_require_complete_carries_the_notes():
    report = _source_report(status="partial", notes=["header context drifted"])
    errors = validate(report, require_complete=True)
    assert any("header context drifted" in error for error in errors)


def test_require_complete_accepts_a_complete_report():
    assert validate(_source_report(), require_complete=True) == []
