from types import SimpleNamespace

import pytest

from scenario_command import run_command


def test_operational_exit_rejects_and_removes_stale_report(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text('{"verdict":"NO_CHANGE"}')
    monkeypatch.setattr(
        "scenario_command.subprocess.run", lambda argv: SimpleNamespace(returncode=64)
    )

    with pytest.raises(RuntimeError, match=r"command failed \(64\)"):
        run_command(["abicheck", "compare"], verdict_report=report)

    assert not report.exists()


def test_verdict_exit_requires_fresh_report(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        "scenario_command.subprocess.run", lambda argv: SimpleNamespace(returncode=4)
    )

    with pytest.raises(RuntimeError, match="produced no report"):
        run_command(["abicheck", "compare"], verdict_report=report)
