import json
import textwrap

import validate_run_plan
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


def _minimal_config() -> str:
    return textwrap.dedent(
        """
        profiles:
          p1: {contract: true}
        targets:
          math:
            checks:
              - channel: accepted-main
                depth: binary
                required: true
                gate_mode: deferred
        """
    )


def _cell(check_id: str = "math@p1#accepted-main@binary") -> dict:
    return {
        "check_id": check_id,
        "required": True,
        "gate_mode": "deferred",
        "requested_depth": "binary",
    }


def _plan(checks: list) -> dict:
    return {"checks": checks, "gate": {"missing_required": "fail", "unexpected_target": "fail"}}


def _run(tmp_path, plan: dict, config: str | None = None) -> list:
    config_path = tmp_path / ".abicheck.yml"
    config_path.write_text(config or _minimal_config(), encoding="utf-8")
    plan_path = tmp_path / "run-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return validate_run_plan.validate(config_path, plan_path)


def test_declared_plan_is_accepted(tmp_path) -> None:
    assert _run(tmp_path, _plan([_cell()])) == []


def test_duplicate_check_ids_are_rejected(tmp_path) -> None:
    """Two cells sharing one id collapse to a single aggregate expectation."""
    errors = _run(tmp_path, _plan([_cell(), _cell()]))
    assert any("duplicate check_id" in error for error in errors)


def test_duplicate_ids_are_caught_even_though_the_id_set_matches(tmp_path) -> None:
    errors = _run(tmp_path, _plan([_cell(), _cell()]))
    assert not any("run-plan cells differ" in error for error in errors)
    assert errors, "a duplicated cell must not validate clean"


def test_non_object_cell_is_rejected_before_it_joins_the_id_set(tmp_path) -> None:
    errors = _run(tmp_path, _plan([_cell(), "math@p1#accepted-main@binary"]))
    assert any("is not an object" in error for error in errors)
    # The malformed cell contributed no identifier, so the set comparison
    # still sees exactly the declared cell rather than a bogus extra one.
    assert not any("run-plan cells differ" in error for error in errors)


def test_cell_without_a_usable_check_id_is_rejected(tmp_path) -> None:
    errors = _run(tmp_path, _plan([_cell(), {"check_id": None}]))
    assert any("invalid check_id" in error for error in errors)


def test_non_list_checks_is_rejected(tmp_path) -> None:
    errors = _run(tmp_path, _plan({"a": 1}))
    assert errors and "must be a list" in errors[0]
