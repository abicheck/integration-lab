from pathlib import Path

from validate_expected_gaps import validate


def test_repository_expected_gaps_are_precise():
    assert validate(Path(__file__).parents[1] / "scenarios" / "manifest.yaml") == []


def test_gap_requires_failure_contract(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("expected_gaps:\n  - id: gap\n    status: expected_gap\n")
    errors = validate(manifest)
    assert any("upstream_issue" in error for error in errors)
    assert any("phase and reason" in error for error in errors)
