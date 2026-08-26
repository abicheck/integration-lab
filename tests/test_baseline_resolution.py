"""Tests for ci/baseline_resolution.py -- the accepted-main baseline receipt."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ci"))

import baseline_resolution as br  # noqa: E402

BASE = "b" * 40
PARENT = "a" * 40
GRANDPARENT = "9" * 40


def _walker(graph: dict[str, str], diffs: dict[tuple[str, str], list[str] | None]):
    return (
        lambda parent, child: diffs.get((parent, child)),
        lambda sha: graph.get(sha),
    )


def _manifest(tmp_path: Path, **overrides) -> Path:
    directory = tmp_path / "baseline-set"
    directory.mkdir(exist_ok=True)
    payload = {
        "profile": "linux-x86_64-gcc14-cxx17-make-bear",
        "project_ref": BASE,
        "baseline_generation": 1,
    }
    payload.update(overrides)
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


# --------------------------------------------------------------------------
# ABI-neutral classification
# --------------------------------------------------------------------------


def test_abi_only_paths_are_neutral() -> None:
    assert br.is_abi_neutral(["abi/math.abicheck.json", "abi/profiles/x/strings.abicheck.json"])


def test_any_non_abi_path_breaks_neutrality() -> None:
    assert not br.is_abi_neutral(["abi/math.abicheck.json", "src/math.cpp"])


def test_empty_change_set_is_not_neutral() -> None:
    """An empty diff means the diff failed, not that the commit was safe."""
    assert not br.is_abi_neutral([])


def test_path_prefix_is_a_directory_not_a_string_prefix() -> None:
    # `abicheck.yml` must not be swallowed by the `abi/` prefix.
    assert not br.is_abi_neutral(["abicheck.yml"])


# --------------------------------------------------------------------------
# Source-commit resolution
# --------------------------------------------------------------------------


def test_exact_base_when_the_base_commit_is_not_a_refresh() -> None:
    changed, parent_of = _walker({BASE: PARENT}, {(PARENT, BASE): ["src/math.cpp"]})
    result = br.resolve_source_commit(BASE, changed, parent_of)
    assert result["selected_source_commit"] == BASE
    assert result["reason_selected"] == br.REASON_EXACT_BASE
    assert result["abi_neutral_commits_skipped"] == []


def test_walks_past_a_bot_baseline_refresh_commit() -> None:
    changed, parent_of = _walker(
        {BASE: PARENT, PARENT: GRANDPARENT},
        {
            (PARENT, BASE): ["abi/math.abicheck.json"],
            (GRANDPARENT, PARENT): ["src/math.cpp"],
        },
    )
    result = br.resolve_source_commit(BASE, changed, parent_of)
    assert result["selected_source_commit"] == PARENT
    assert result["reason_selected"] == br.REASON_ABI_EQUIVALENT_ANCESTOR
    assert [entry["commit"] for entry in result["abi_neutral_commits_skipped"]] == [BASE]


def test_walk_stops_at_the_root_commit() -> None:
    changed, parent_of = _walker({BASE: PARENT}, {(PARENT, BASE): ["abi/math.abicheck.json"]})
    result = br.resolve_source_commit(BASE, changed, parent_of)
    assert result["selected_source_commit"] == PARENT


def test_walk_stops_when_the_diff_cannot_be_computed() -> None:
    changed, parent_of = _walker({BASE: PARENT}, {(PARENT, BASE): None})
    result = br.resolve_source_commit(BASE, changed, parent_of)
    assert result["selected_source_commit"] == BASE


def test_walk_is_bounded_and_reports_truncation() -> None:
    graph = {f"{i:040x}": f"{i + 1:040x}" for i in range(200)}
    diffs = {(v, k): ["abi/x.json"] for k, v in graph.items()}
    changed, parent_of = _walker(graph, diffs)
    result = br.resolve_source_commit(f"{0:040x}", changed, parent_of, max_steps=5)
    assert result["walk_truncated"] is True
    assert len(result["abi_neutral_commits_skipped"]) == 5


def test_missing_requested_base_is_an_error() -> None:
    with pytest.raises(br.ResolutionError):
        br.resolve_source_commit("", lambda a, b: [], lambda s: None)


# --------------------------------------------------------------------------
# Receipt classification
# --------------------------------------------------------------------------


def _classify(tmp_path: Path, **overrides):
    kwargs = dict(
        requested_pr_base=BASE,
        selected_source_commit=BASE,
        profile="linux-x86_64-gcc14-cxx17-make-bear",
        expected_generation=1,
        cache_primary_key=f"integration-lab-main-g1-p-{BASE}",
        cache_matched_key=f"integration-lab-main-g1-p-{BASE}",
        baseline_dir=tmp_path / "baseline-set",
    )
    kwargs.update(overrides)
    return br.classify(**kwargs)


def test_receipt_carries_every_required_identity(tmp_path: Path) -> None:
    _manifest(tmp_path)
    receipt = _classify(tmp_path)
    for field in (
        "requested_pr_base",
        "selected_source_commit",
        "cache_primary_key",
        "cache_matched_key",
        "manifest_project_ref",
        "manifest_generation",
        "reason_selected",
    ):
        assert field in receipt, field
    assert receipt["usable"] is True
    assert receipt["reason_selected"] == br.REASON_EXACT_BASE


def test_ancestor_baseline_is_accepted_and_named(tmp_path: Path) -> None:
    _manifest(tmp_path, project_ref=PARENT)
    receipt = _classify(tmp_path, selected_source_commit=PARENT)
    assert receipt["usable"] is True
    assert receipt["reason_selected"] == br.REASON_ABI_EQUIVALENT_ANCESTOR
    assert receipt["manifest_project_ref"] == PARENT


def test_cache_miss_is_receipted_not_a_traceback(tmp_path: Path) -> None:
    receipt = _classify(tmp_path, cache_matched_key="")
    assert receipt["usable"] is False
    assert receipt["reason_selected"] == br.REJECT_CACHE_MISS


def test_prefix_fallback_onto_an_unrelated_commit_is_rejected(tmp_path: Path) -> None:
    """restore-keys can match any older cache; identity still decides."""
    _manifest(tmp_path, project_ref=GRANDPARENT)
    receipt = _classify(
        tmp_path, cache_matched_key=f"integration-lab-main-g1-p-{GRANDPARENT}"
    )
    assert receipt["usable"] is False
    assert receipt["reason_selected"] == br.REJECT_WRONG_PROJECT_REF
    assert receipt["manifest_project_ref"] == GRANDPARENT


def test_wrong_profile_is_rejected(tmp_path: Path) -> None:
    _manifest(tmp_path, profile="linux-x86_64-gcc14-cxx17-bazel")
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_WRONG_PROFILE


def test_stale_generation_is_rejected(tmp_path: Path) -> None:
    _manifest(tmp_path, baseline_generation=0)
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_STALE_GENERATION
    assert receipt["manifest_generation"] == 0


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "baseline-set").mkdir()
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_MISSING_MANIFEST


def test_malformed_manifest_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "baseline-set"
    directory.mkdir()
    (directory / "manifest.json").write_text("{not json", encoding="utf-8")
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_MALFORMED_MANIFEST


def test_byte_corrupted_manifest_is_rejected_not_decoded(tmp_path: Path) -> None:
    directory = tmp_path / "baseline-set"
    directory.mkdir()
    (directory / "manifest.json").write_bytes(b'{"profile": "\xff\xfe"}')
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_MALFORMED_MANIFEST


def test_non_object_manifest_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "baseline-set"
    directory.mkdir()
    (directory / "manifest.json").write_text("[]", encoding="utf-8")
    receipt = _classify(tmp_path)
    assert receipt["reason_selected"] == br.REJECT_MALFORMED_MANIFEST


def test_rebuilt_baseline_is_named_distinctly(tmp_path: Path) -> None:
    """A rebuild must never be receipted as if a cache had served it."""
    _manifest(tmp_path)
    receipt = _classify(tmp_path, cache_matched_key="", rebuilt=True)
    assert receipt["usable"] is True
    assert receipt["reason_selected"] == br.REASON_REBUILT
    assert receipt["cache_matched_key"] is None


def test_rebuilt_baseline_still_has_to_match_its_source_commit(tmp_path: Path) -> None:
    _manifest(tmp_path, project_ref=GRANDPARENT)
    receipt = _classify(tmp_path, cache_matched_key="", rebuilt=True)
    assert receipt["usable"] is False
    assert receipt["reason_selected"] == br.REJECT_WRONG_PROJECT_REF


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_classify_cli_fails_closed_and_writes_the_receipt(tmp_path: Path) -> None:
    _manifest(tmp_path, project_ref=GRANDPARENT)
    out = tmp_path / "receipt.json"
    code = br.main(
        [
            "classify",
            "--requested", BASE,
            "--selected", BASE,
            "--profile", "linux-x86_64-gcc14-cxx17-make-bear",
            "--primary-key", "k",
            "--matched-key", "k",
            "--baseline-dir", str(tmp_path / "baseline-set"),
            "--out", str(out),
        ]
    )
    assert code == 1
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["reason_selected"] == br.REJECT_WRONG_PROJECT_REF
    assert receipt["manifest_project_ref"] == GRANDPARENT


def test_classify_cli_no_fail_still_records_the_verdict(tmp_path: Path) -> None:
    (tmp_path / "baseline-set").mkdir()
    out = tmp_path / "receipt.json"
    code = br.main(
        [
            "classify",
            "--requested", BASE,
            "--selected", BASE,
            "--profile", "p",
            "--primary-key", "k",
            "--baseline-dir", str(tmp_path / "baseline-set"),
            "--out", str(out),
            "--no-fail",
        ]
    )
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["usable"] is False


def test_resolve_cli_walks_a_real_git_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "lab@example.invalid")
    git("config", "user.name", "lab")
    (repo / "src").mkdir()
    (repo / "src" / "math.cpp").write_text("int f();\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "source")
    source_commit = git("rev-parse", "HEAD")
    (repo / "abi").mkdir()
    (repo / "abi" / "math.abicheck.json").write_text("{}\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "chore: refresh ABICheck baseline")
    refresh_commit = git("rev-parse", "HEAD")

    out = tmp_path / "resolution.json"
    assert br.main(["resolve", "--requested", refresh_commit, "--repo", str(repo),
                    "--out", str(out)]) == 0
    resolution = json.loads(out.read_text(encoding="utf-8"))
    assert resolution["selected_source_commit"] == source_commit
    assert resolution["reason_selected"] == br.REASON_ABI_EQUIVALENT_ANCESTOR


def test_resolve_cli_keeps_a_source_change_exact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "lab@example.invalid")
    git("config", "user.name", "lab")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "one")
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "two")
    head = git("rev-parse", "HEAD")
    assert br.main(["resolve", "--requested", head, "--repo", str(repo)]) == 0


# --- generator identity (Codex review) ----------------------------------
#
# The candidate side of every comparison is scanned with the workflow's
# CURRENT pin, but a restored cache holds a baseline generated by whatever pin
# was current when it was written. Classification checked profile, project ref
# and generation but never the generator, so after a pin bump the comparison
# depended on cache availability: a surviving cache compared an old-generator
# baseline against a new-generator candidate, an expired one rebuilt with the
# new pin and compared like for like. Rejecting here is cheap -- it falls
# through to the rebuild path rather than turning the gate red.

PIN = "42da2d2f947d9eaa42e7d5f334bd2098bb5f08e7"
OLD_PIN = "6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"


def _classify_with(tmp_path, *, recorded, expected, rebuilt=False):
    kwargs = {}
    if recorded is not None:
        kwargs["generator_git_sha"] = recorded
    directory = _manifest(tmp_path, **kwargs)
    return br.classify(
        requested_pr_base=BASE,
        selected_source_commit=BASE,
        profile="linux-x86_64-gcc14-cxx17-make-bear",
        expected_generation=1,
        cache_primary_key="k",
        cache_matched_key="k",
        baseline_dir=directory,
        rebuilt=rebuilt,
        expected_generator=expected,
    )


def test_matching_generator_is_usable(tmp_path):
    receipt = _classify_with(tmp_path, recorded=PIN, expected=PIN)
    assert receipt["usable"]
    assert receipt["manifest_generator_git_sha"] == PIN
    assert receipt["expected_generator_git_sha"] == PIN


def test_a_baseline_from_a_different_pin_is_rejected(tmp_path):
    receipt = _classify_with(tmp_path, recorded=OLD_PIN, expected=PIN)
    assert not receipt["usable"]
    assert receipt["reason_selected"] == br.REJECT_WRONG_GENERATOR
    # The receipt must name both sides, or a red job says nothing useful.
    assert receipt["manifest_generator_git_sha"] == OLD_PIN
    assert receipt["expected_generator_git_sha"] == PIN


def test_a_baseline_with_no_recorded_generator_is_rejected(tmp_path):
    """Unknown is not "matching" -- a cache written before this field existed
    records an unknown extraction, and that is the hazard itself."""
    receipt = _classify_with(tmp_path, recorded=None, expected=PIN)
    assert not receipt["usable"]
    assert receipt["reason_selected"] == br.REJECT_UNKNOWN_GENERATOR


def test_an_empty_recorded_generator_is_rejected(tmp_path):
    receipt = _classify_with(tmp_path, recorded="", expected=PIN)
    assert receipt["reason_selected"] == br.REJECT_UNKNOWN_GENERATOR


def test_the_generator_check_is_skipped_when_no_expectation_is_given(tmp_path):
    """Back-compat for callers that do not pass one; the workflow always does,
    and test_the_workflow_passes_the_pin_to_both_classify_passes pins that."""
    receipt = _classify_with(tmp_path, recorded=None, expected=None)
    assert receipt["usable"]
    assert receipt["expected_generator_git_sha"] is None


def test_a_rebuilt_baseline_is_still_generator_checked(tmp_path):
    """The rebuild path supplies the pin, so a mismatch there is a real bug
    rather than a stale cache -- it must not be waved through."""
    receipt = _classify_with(tmp_path, recorded=OLD_PIN, expected=PIN, rebuilt=True)
    assert not receipt["usable"]
    assert receipt["reason_selected"] == br.REJECT_WRONG_GENERATOR


def test_profile_and_project_ref_are_still_checked_first(tmp_path):
    """A wrong profile must not be reported as a generator problem."""
    directory = _manifest(tmp_path, profile="someone-elses-profile",
                          generator_git_sha=OLD_PIN)
    receipt = br.classify(
        requested_pr_base=BASE, selected_source_commit=BASE,
        profile="linux-x86_64-gcc14-cxx17-make-bear", expected_generation=1,
        cache_primary_key="k", cache_matched_key="k", baseline_dir=directory,
        expected_generator=PIN,
    )
    assert receipt["reason_selected"] == br.REJECT_WRONG_PROFILE


def test_the_workflow_passes_the_pin_to_both_classify_passes():
    """The check is opt-in, so the workflow opting in is the thing to pin."""
    import yaml

    root = Path(__file__).resolve().parent.parent
    document = yaml.safe_load((root / ".github/workflows/project-shadow.yml").read_text())
    assert document["env"]["ABICHECK_REF"], "the workflow-level pin is what is passed"
    steps = [
        step for step in document["jobs"]["restore-baseline"]["steps"]
        if isinstance(step.get("run"), str)
        and "baseline_resolution.py classify" in step["run"]
    ]
    assert len(steps) == 2, "expected a --no-fail pass and a fail-closed pass"
    for step in steps:
        assert "--expected-generator" in step["run"], step.get("name")
        assert "$ABICHECK_REF" in step["run"], step.get("name")
