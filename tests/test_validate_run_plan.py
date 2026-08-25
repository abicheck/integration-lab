import json
from validate_run_plan import validate


def test_complete_advisory_plan_passes(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "targets:\n  core:\n    checks: [{channel: accepted-main, depth: binary, "
        "required: true, gate_mode: deferred}]\n"
        "profiles:\n  p: {contract: true}\n"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "checks": [{"check_id": "core@p#accepted-main@binary", "required": True,
                    "gate_mode": "deferred", "requested_depth": "binary"}],
        "gate": {"missing_required": "fail", "unexpected_target": "fail"},
    }))
    assert validate(config, plan) == []


def test_missing_cell_fails(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("targets:\n  core: {checks: [{}]}\nprofiles:\n  p: {}\n")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"checks": [], "gate": {}}))
    assert validate(config, plan)


def test_mixed_depth_profile_scoping_and_policy_pass(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "targets:\n"
        "  core:\n"
        "    checks:\n"
        "      - {channel: accepted-main, depth: headers, required: true, "
        "gate_mode: deferred, profiles: [gcc]}\n"
        "      - {channel: release, depth: source, required: false, "
        "gate_mode: immediate, profiles: [clang]}\n"
        "profiles:\n  gcc: {}\n  clang: {}\n"
        "aggregate:\n  gate: {missing_required: fail, unexpected_target: fail}\n"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "checks": [
            {"check_id": "core@gcc#accepted-main@headers", "required": True,
             "gate_mode": "deferred", "requested_depth": "headers"},
            {"check_id": "core@clang#release@source", "required": False,
             "gate_mode": "immediate", "requested_depth": "source"},
        ],
        "gate": {"missing_required": "fail", "unexpected_target": "fail"},
    }))
    assert validate(config, plan) == []


def test_declared_depth_and_gate_policy_must_be_preserved(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "targets:\n  core:\n    checks:\n"
        "      - {channel: accepted-main, depth: source, required: false, "
        "gate_mode: immediate}\n"
        "profiles:\n  p: {}\n"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "checks": [{"check_id": "core@p#accepted-main@source", "required": True,
                    "gate_mode": "deferred", "requested_depth": "binary"}],
        "gate": {"missing_required": "fail", "unexpected_target": "fail"},
    }))
    errors = validate(config, plan)
    assert any("required=True" in error for error in errors)
    assert any("gate_mode='deferred'" in error for error in errors)
    assert any("requested_depth='binary'" in error for error in errors)


def test_missing_check_id_is_reported_without_sorting_error(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "targets:\n  core:\n    checks: [{channel: accepted-main, depth: binary}]\n"
        "profiles:\n  p: {}\n"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "checks": [{"check_id": None, "requested_depth": "binary"}],
        "gate": {"missing_required": "fail", "unexpected_target": "fail"},
    }))

    errors = validate(config, plan)
    assert any("invalid check_id" in error for error in errors)
    assert any("run-plan cells differ" in error for error in errors)
