"""Unit tests for ci/select_profiles.py -- resolving ci/profiles.yaml +
ci/event-policy.yaml into a matrix-ready profile list per event.
"""
from __future__ import annotations

import textwrap

import pytest

from select_profiles import SelectionError, load_policy, load_profiles, select

REPO_PROFILES = None  # populated by fixture below


@pytest.fixture()
def repo_paths():
    from pathlib import Path

    ci_dir = Path(__file__).resolve().parent.parent / "ci"
    return ci_dir / "profiles.yaml", ci_dir / "event-policy.yaml"


def _write_profiles(tmp_path, ids=("p-a", "p-b", "p-c")):
    profiles = "\n".join(
        f"""  - id: {pid}
    backend: bazel
    contract: {"true" if pid == ids[0] else "false"}
    root: "."
    targets: {{}}
    header_roots: []
"""
        for pid in ids
    )
    path = tmp_path / "profiles.yaml"
    path.write_text(f"schema_version: 1\nprofiles:\n{profiles}")
    return path


def _write_policy(tmp_path, required=None, advisory=None, profile_sets=None):
    required = required or []
    advisory = advisory or []
    profile_sets = profile_sets or {}
    doc = {
        "schema_version": 1,
        "profile_sets": profile_sets,
        "events": {
            "pull_request": {"required": required, "advisory": advisory},
        },
    }
    import yaml

    path = tmp_path / "event-policy.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def test_real_profiles_and_policy_load_and_select_pull_request(repo_paths):
    profiles_path, policy_path = repo_paths
    result = select("pull_request", profiles_path, policy_path)
    assert result["event"] == "pull_request"
    assert "linux-x86_64-gcc14-cxx17-bazel" in result["profiles"]
    assert "linux-x86_64-gcc14-cxx17-cmake-ninja" in result["profiles"]
    assert "linux-x86_64-gcc14-cxx17-make-bear" in result["profiles"]
    # Every profile id in `required`/`advisory` also appears in `profiles`.
    assert set(result["required"]) | set(result["advisory"]) == set(result["profiles"])


def test_real_profiles_and_policy_select_main(repo_paths):
    profiles_path, policy_path = repo_paths
    result = select("main", profiles_path, policy_path)
    assert result["profiles"]


def test_load_profiles_rejects_empty_manifest(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("schema_version: 1\nprofiles: []\n")
    with pytest.raises(SelectionError):
        load_profiles(path)


def test_select_expands_profile_set(tmp_path):
    profiles_path = _write_profiles(tmp_path)
    policy_path = _write_policy(
        tmp_path,
        required=["group-a"],
        advisory=["p-c"],
        profile_sets={"group-a": ["p-a", "p-b"]},
    )
    result = select("pull_request", profiles_path, policy_path)
    assert result["required"] == ["p-a", "p-b"]
    assert result["advisory"] == ["p-c"]
    assert result["profiles"] == ["p-a", "p-b", "p-c"]


def test_select_required_and_advisory_deduplicated(tmp_path):
    profiles_path = _write_profiles(tmp_path)
    policy_path = _write_policy(tmp_path, required=["p-a"], advisory=["p-a", "p-b"])
    result = select("pull_request", profiles_path, policy_path)
    # p-a is required; it must not also show up in advisory.
    assert result["required"] == ["p-a"]
    assert result["advisory"] == ["p-b"]


def test_select_unknown_event_raises(tmp_path):
    profiles_path = _write_profiles(tmp_path)
    policy_path = _write_policy(tmp_path)
    with pytest.raises(SelectionError):
        select("nonexistent_event", profiles_path, policy_path)


def test_select_unknown_profile_set_reference_raises(tmp_path):
    profiles_path = _write_profiles(tmp_path)
    policy_path = _write_policy(tmp_path, required=["does-not-exist"])
    with pytest.raises(SelectionError):
        select("pull_request", profiles_path, policy_path)


def test_cli_main_prints_json(repo_paths, capsys):
    from select_profiles import main

    profiles_path, policy_path = repo_paths
    rc = main(
        [
            "--event",
            "pull_request",
            "--profiles-file",
            str(profiles_path),
            "--policy-file",
            str(policy_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    import json

    doc = json.loads(out)
    assert doc["event"] == "pull_request"


def test_cli_main_profile_filter_narrows_output(repo_paths, capsys):
    from select_profiles import main

    profiles_path, policy_path = repo_paths
    rc = main(
        [
            "--event",
            "pull_request",
            "--profiles-file",
            str(profiles_path),
            "--policy-file",
            str(policy_path),
            "--profile",
            "linux-x86_64-gcc14-cxx17-bazel",
        ]
    )
    assert rc == 0
    import json

    doc = json.loads(capsys.readouterr().out)
    assert doc["profiles"] == ["linux-x86_64-gcc14-cxx17-bazel"]


def test_cli_main_unknown_profile_filter_fails(repo_paths, capsys):
    from select_profiles import main

    profiles_path, policy_path = repo_paths
    rc = main(
        [
            "--event",
            "pull_request",
            "--profiles-file",
            str(profiles_path),
            "--policy-file",
            str(policy_path),
            "--profile",
            "does-not-exist",
        ]
    )
    assert rc == 1
