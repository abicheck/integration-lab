"""Unit tests for scripts/resolve_scanner_candidate.py (PR5 scanner
candidate resolution + repository_dispatch payload validation, design doc
section 23.3)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from resolve_scanner_candidate import (
    ResolutionError,
    resolve_ref_to_sha,
    resolve_named_ref,
    validate_dispatch_payload,
    validate_repository,
    write_github_output,
)

VALID_SHA = "a" * 40
OTHER_SHA = "b" * 40


def test_validate_dispatch_payload_accepts_minimal_valid_payload():
    result = validate_dispatch_payload({"scanner_repository": "abicheck/abicheck", "scanner_sha": VALID_SHA})
    assert result["scanner_repository"] == "abicheck/abicheck"
    assert result["scanner_sha"] == VALID_SHA
    assert result["scanner_version"] is None
    assert result["baseline_generation"] is None
    assert result["pip_spec"] == f"abicheck @ git+https://github.com/abicheck/abicheck.git@{VALID_SHA}"
    assert result["source_event"] == "repository_dispatch"


def test_validate_dispatch_payload_accepts_optional_fields():
    result = validate_dispatch_payload(
        {
            "scanner_repository": "abicheck/abicheck",
            "scanner_sha": VALID_SHA,
            "scanner_version": "0.6.0",
            "baseline_generation": "2026-08",
        }
    )
    assert result["scanner_version"] == "0.6.0"
    assert result["baseline_generation"] == "2026-08"


def test_validate_dispatch_payload_allows_fork_repository():
    result = validate_dispatch_payload({"scanner_repository": "someone/abicheck-fork", "scanner_sha": VALID_SHA})
    assert result["scanner_repository"] == "someone/abicheck-fork"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"scanner_sha": VALID_SHA},
        {"scanner_repository": "", "scanner_sha": VALID_SHA},
        {"scanner_repository": 123, "scanner_sha": VALID_SHA},
    ],
)
def test_validate_dispatch_payload_rejects_missing_or_invalid_repository(payload):
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(payload)


@pytest.mark.parametrize(
    "sha",
    [
        None,
        "",
        "main",
        "a" * 39,
        "a" * 41,
        "g" * 40,  # not hex
        "A" * 40,  # uppercase not accepted (git's own canonical form is lowercase)
    ],
)
def test_validate_dispatch_payload_rejects_missing_or_non_sha(sha):
    payload = {"scanner_repository": "abicheck/abicheck"}
    if sha is not None:
        payload["scanner_sha"] = sha
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(payload)


def test_validate_dispatch_payload_rejects_non_string_optional_fields():
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(
            {"scanner_repository": "abicheck/abicheck", "scanner_sha": VALID_SHA, "scanner_version": 5}
        )
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(
            {"scanner_repository": "abicheck/abicheck", "scanner_sha": VALID_SHA, "baseline_generation": []}
        )


def test_validate_dispatch_payload_rejects_non_dict():
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(["not", "a", "dict"])


# CodeRabbit review, PR #24 (Critical): scanner_repository shape validation
# and $GITHUB_OUTPUT injection guard.


@pytest.mark.parametrize(
    "repository",
    [
        "abicheck/abicheck",
        "some-org/abicheck-fork",
        "a/b",
        "org.name/repo_name.git",
    ],
)
def test_validate_repository_accepts_owner_repo_shapes(repository):
    assert validate_repository(repository, "field") == repository


@pytest.mark.parametrize(
    "repository",
    [
        None,
        123,
        "",
        "no-slash-at-all",
        "abicheck/abicheck/extra",
        "abicheck/abicheck\n",
        "abicheck/abi check",
        "abicheck/abi;check",
        "abicheck/$(whoami)",
        "abicheck/abi`check`",
        "/abicheck",
        "abicheck/",
    ],
)
def test_validate_repository_rejects_malformed_or_injection_shaped_values(repository):
    with pytest.raises(ResolutionError):
        validate_repository(repository, "field")


def test_validate_dispatch_payload_rejects_malformed_repository_shape():
    with pytest.raises(ResolutionError):
        validate_dispatch_payload({"scanner_repository": "abicheck/abicheck\ninjected=x", "scanner_sha": VALID_SHA})


def test_validate_dispatch_payload_rejects_scanner_version_with_embedded_newline():
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(
            {
                "scanner_repository": "abicheck/abicheck",
                "scanner_sha": VALID_SHA,
                "scanner_version": "0.6.0\ninjected=malicious",
            }
        )


def test_validate_dispatch_payload_rejects_baseline_generation_with_carriage_return():
    with pytest.raises(ResolutionError):
        validate_dispatch_payload(
            {
                "scanner_repository": "abicheck/abicheck",
                "scanner_sha": VALID_SHA,
                "baseline_generation": "2026-08\rinjected=malicious",
            }
        )


def test_resolve_named_ref_rejects_malformed_repository():
    with pytest.raises(ResolutionError):
        resolve_named_ref("not a repo\ninjected", "main", "schedule", runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not shell out")))


def test_write_github_output_writes_expected_lines(tmp_path):
    result = {
        "scanner_repository": "abicheck/abicheck",
        "scanner_sha": VALID_SHA,
        "scanner_version": "0.6.0",
        "baseline_generation": "2026-08",
        "pip_spec": f"abicheck @ git+https://github.com/abicheck/abicheck.git@{VALID_SHA}",
    }
    out = tmp_path / "github_output"
    out.write_text("existing=line\n", encoding="utf-8")
    write_github_output(result, out)
    content = out.read_text(encoding="utf-8")
    assert content == (
        "existing=line\n"
        "scanner_repository=abicheck/abicheck\n"
        f"scanner_sha={VALID_SHA}\n"
        "scanner_version=0.6.0\n"
        f"pip_spec=abicheck @ git+https://github.com/abicheck/abicheck.git@{VALID_SHA}\n"
    )


def test_write_github_output_writes_empty_string_for_none_scanner_version(tmp_path):
    result = {
        "scanner_repository": "abicheck/abicheck",
        "scanner_sha": VALID_SHA,
        "scanner_version": None,
        "pip_spec": f"abicheck @ git+https://github.com/abicheck/abicheck.git@{VALID_SHA}",
    }
    out = tmp_path / "github_output"
    write_github_output(result, out)
    assert "scanner_version=\n" in out.read_text(encoding="utf-8")


def test_write_github_output_rejects_embedded_newline():
    result = {"scanner_repository": "abicheck/abicheck", "scanner_sha": VALID_SHA, "scanner_version": "x\ninjected=y", "pip_spec": "spec"}
    with pytest.raises(ResolutionError):
        write_github_output(result, Path("/dev/null"))


def test_write_github_output_rejects_embedded_carriage_return():
    result = {"scanner_repository": "abicheck/abicheck", "scanner_sha": VALID_SHA, "scanner_version": "x\rinjected=y", "pip_spec": "spec"}
    with pytest.raises(ResolutionError):
        write_github_output(result, Path("/dev/null"))


def test_resolve_ref_to_sha_returns_sha_unchanged_without_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should not shell out for an already-exact SHA")

    assert resolve_ref_to_sha("abicheck/abicheck", VALID_SHA, runner=_boom) == VALID_SHA


def test_resolve_ref_to_sha_parses_ls_remote_output():
    def _fake_runner(cmd, **kwargs):
        assert cmd[:2] == ["git", "ls-remote"]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{OTHER_SHA}\trefs/heads/main\n", stderr="")

    assert resolve_ref_to_sha("abicheck/abicheck", "main", runner=_fake_runner) == OTHER_SHA


def test_resolve_ref_to_sha_takes_first_line_when_multiple_matches():
    def _fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"{OTHER_SHA}\trefs/tags/v0.5.0\n{VALID_SHA}\trefs/tags/v0.5.0^{{}}\n",
            stderr="",
        )

    assert resolve_ref_to_sha("abicheck/abicheck", "v0.5.0", runner=_fake_runner) == OTHER_SHA


def test_resolve_ref_to_sha_raises_on_nonzero_exit():
    def _fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: could not read")

    with pytest.raises(ResolutionError):
        resolve_ref_to_sha("abicheck/abicheck", "main", runner=_fake_runner)


def test_resolve_ref_to_sha_raises_on_empty_output():
    def _fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(ResolutionError):
        resolve_ref_to_sha("abicheck/abicheck", "no-such-ref", runner=_fake_runner)


def test_resolve_ref_to_sha_raises_on_malformed_line():
    def _fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="not-a-sha\trefs/heads/main\n", stderr="")

    with pytest.raises(ResolutionError):
        resolve_ref_to_sha("abicheck/abicheck", "main", runner=_fake_runner)


def test_resolve_named_ref_wraps_result_shape():
    def _fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{OTHER_SHA}\trefs/heads/main\n", stderr="")

    result = resolve_named_ref("abicheck/abicheck", "main", "schedule", runner=_fake_runner)
    assert result == {
        "source_event": "schedule",
        "scanner_repository": "abicheck/abicheck",
        "scanner_sha": OTHER_SHA,
        "scanner_version": None,
        "baseline_generation": None,
        "pip_spec": f"abicheck @ git+https://github.com/abicheck/abicheck.git@{OTHER_SHA}",
    }
