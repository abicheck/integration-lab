import json
from validate_run_plan import validate


def test_complete_advisory_plan_passes(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "targets:\n  core:\n    checks: [{channel: accepted-main, depth: binary}]\n"
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
