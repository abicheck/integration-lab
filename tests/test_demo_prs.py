"""Tests for the generated demonstration PRs (roadmap item 14).

Two things are under test: the oracle's judgement of a gate report against a
declared expectation, and the generator's manifest/body handling. Git
operations are not exercised here -- `--check` reads real refs and is run
directly by an operator (see docs/operations.md); what these tests pin is
everything that decides whether a demonstration still demonstrates its claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import check_demo_oracle as oracle
import gen_demo_prs as gen

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "demos" / "manifest.yaml"


def _demo(**expect):
    base = {"gate": "green", "verdict": "COMPATIBLE_WITH_RISK",
            "required_findings": [], "forbidden_findings": []}
    base.update(expect)
    return {"id": "d", "branch": "test/d", "title": "t", "claim": "c",
            "patch": "p", "expect": base}


def _report(verdict="COMPATIBLE_WITH_RISK", changes=(), operational=None):
    report = {"verdict": verdict, "changes": list(changes)}
    if operational:
        report["operational_errors"] = list(operational)
    return report


# --- the oracle ---------------------------------------------------------


def test_matching_natural_result_is_green():
    assert oracle.evaluate(_demo(), _report()) == []


def test_a_different_verdict_fails():
    errors = oracle.evaluate(_demo(), _report(verdict="BREAKING"))
    assert any("verdict=" in e for e in errors)


def test_gate_colour_is_derived_from_the_verdict_not_declared():
    """A manifest cannot claim green for a breaking demo."""
    errors = oracle.evaluate(_demo(gate="green", verdict="BREAKING"),
                             _report(verdict="BREAKING"))
    assert any("gate=red" in e for e in errors)
    assert not any("verdict=" in e for e in errors)


def test_missing_required_finding_fails():
    demo = _demo(required_findings=[{"kind": "func_added", "symbol": "_Z1fv"}])
    errors = oracle.evaluate(demo, _report(changes=[{"kind": "func_added", "symbol": "_Z1gv"}]))
    assert any("missing required finding" in e for e in errors)


def test_required_finding_present_passes():
    demo = _demo(required_findings=[{"kind": "func_added", "symbol": "_Z1fv"}])
    assert oracle.evaluate(demo, _report(changes=[{"kind": "func_added", "symbol": "_Z1fv"}])) == []


def test_kind_only_matcher_forbids_every_symbol_of_that_kind():
    """This is what makes the source-only break stay source-only."""
    demo = _demo(forbidden_findings=[{"kind": "func_removed"}])
    errors = oracle.evaluate(demo, _report(changes=[{"kind": "func_removed", "symbol": "_Z1anything"}]))
    assert any("forbidden finding" in e and "_Z1anything" in e for e in errors)


def test_forbidden_matcher_with_a_symbol_does_not_forbid_other_symbols():
    demo = _demo(forbidden_findings=[{"kind": "func_removed", "symbol": "_Z1fv"}])
    assert oracle.evaluate(demo, _report(changes=[{"kind": "func_removed", "symbol": "_Z1gv"}])) == []


def test_unrelated_extra_findings_do_not_fail_a_demo():
    """Findings are required/forbidden, not exact set equality -- a new
    advisory kind must not turn every demonstration red at once."""
    demo = _demo(required_findings=[{"kind": "func_added", "symbol": "_Z1fv"}])
    report = _report(changes=[
        {"kind": "func_added", "symbol": "_Z1fv"},
        {"kind": "some_future_advisory_kind", "symbol": "_Z1fv"},
    ])
    assert oracle.evaluate(demo, report) == []


def test_operational_errors_fail_even_on_the_right_verdict():
    errors = oracle.evaluate(_demo(), _report(operational=["castxml exploded"]))
    assert any("operational error" in e for e in errors)


def test_a_report_with_no_changes_list_is_an_oracle_error():
    with pytest.raises(oracle.OracleError):
        oracle.evaluate(_demo(), {"verdict": "COMPATIBLE_WITH_RISK"})


def test_non_dict_changes_entries_are_ignored_not_crashed_on():
    demo = _demo(required_findings=[{"kind": "func_added", "symbol": "_Z1fv"}])
    report = _report(changes=["junk", None, {"kind": "func_added", "symbol": "_Z1fv"}])
    assert oracle.evaluate(demo, report) == []


def test_unknown_demo_id_is_an_error(tmp_path):
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump({"demonstrations": [{"id": "a", "branch": "test/a"}]}))
    with pytest.raises(oracle.OracleError) as exc:
        oracle.load_demo(m, "nope")
    assert "known ids: a" in str(exc.value)


def test_a_non_demonstration_branch_resolves_to_none(tmp_path):
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump({"demonstrations": [{"id": "a", "branch": "test/a"}]}))
    assert oracle.demo_for_branch(m, "feature/whatever") is None


def test_cli_exits_zero_on_a_non_demonstration_branch(tmp_path, capsys):
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump({"demonstrations": [{"id": "a", "branch": "test/a"}]}))
    report = tmp_path / "r.json"
    report.write_text(json.dumps(_report()))
    assert oracle.main(["--manifest", str(m), "--branch", "feature/x", "--report", str(report)]) == 0


def test_cli_errors_when_the_gate_produced_no_report(tmp_path):
    """A demonstration whose gate never ran cannot be confirmed."""
    assert oracle.main([
        "--manifest", str(MANIFEST), "--demo", "binary-abi-break",
        "--report", str(tmp_path / "absent.json"),
    ]) == 2


def test_cli_returns_one_on_a_drifted_demonstration(tmp_path):
    report = tmp_path / "r.json"
    report.write_text(json.dumps(_report(verdict="NO_CHANGE")))
    assert oracle.main([
        "--manifest", str(MANIFEST), "--demo", "binary-abi-break", "--report", str(report),
    ]) == 1


# --- the real manifest --------------------------------------------------


def test_every_declared_patch_exists_and_is_nonempty():
    for demo in gen.load_manifest(MANIFEST):
        patch = REPO_ROOT / demo["patch"]
        assert patch.is_file() and patch.stat().st_size, demo["id"]


def test_every_demonstration_declares_a_consistent_gate_colour():
    """The manifest's own `gate` must agree with its own verdict."""
    for demo in gen.load_manifest(MANIFEST):
        expect = demo["expect"]
        derived = "red" if expect["verdict"] in oracle.RED_VERDICTS else "green"
        assert expect["gate"] == derived, demo["id"]


def test_the_source_only_break_forbids_binary_findings():
    """The one property that makes it the L4-only case rather than a dup."""
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "source-api-break")
    forbidden = {f["kind"] for f in demo["expect"]["forbidden_findings"]}
    assert {"func_added", "func_removed"} <= forbidden
    assert demo["expect"]["verdict"] == "API_BREAK"


def test_the_binary_break_requires_both_halves_of_the_symbol_swap():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    kinds = {f["kind"] for f in demo["expect"]["required_findings"]}
    assert kinds == {"func_added", "func_removed"}


def test_branches_and_ids_are_unique():
    demos = gen.load_manifest(MANIFEST)
    assert len({d["branch"] for d in demos}) == len(demos)
    assert len({d["id"] for d in demos}) == len(demos)


def test_a_manifest_with_a_duplicate_id_is_rejected(tmp_path):
    m = tmp_path / "m.yaml"
    entry = {"id": "a", "branch": "test/a", "title": "t",
             "patch": "demos/patches/binary-abi-break.patch", "expect": {}}
    m.write_text(yaml.safe_dump({"demonstrations": [entry, dict(entry)]}))
    with pytest.raises(gen.DemoError) as exc:
        gen.load_manifest(m)
    assert "duplicate" in str(exc.value)


def test_a_manifest_pointing_at_a_missing_patch_is_rejected(tmp_path):
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump({"demonstrations": [
        {"id": "a", "branch": "test/a", "title": "t",
         "patch": "demos/patches/does-not-exist.patch", "expect": {}}]}))
    with pytest.raises(gen.DemoError):
        gen.load_manifest(m)


# --- PR body rendering --------------------------------------------------


def test_body_round_trips_between_the_markers():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    first = gen.render_body(demo)
    second = gen.render_body(demo, first)
    assert first.strip() == second.strip()


def test_body_preserves_human_text_outside_the_markers():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    existing = "Context a human wrote.\n\n" + gen.render_body(demo) + "\nA trailing human note.\n"
    updated = gen.render_body(demo, existing)
    assert updated.startswith("Context a human wrote.")
    assert updated.rstrip().endswith("A trailing human note.")


def test_body_appends_the_section_when_no_markers_are_present():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "implementation-only")
    updated = gen.render_body(demo, "Just some prose.")
    assert "Just some prose." in updated
    assert gen.BODY_BEGIN in updated and gen.BODY_END in updated


def test_body_states_the_expected_verdict_and_gate_colour():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "source-api-break")
    body = gen.render_body(demo)
    assert "API_BREAK" in body and "red" in body
    assert "method_access_changed" in body


def test_body_says_none_rather_than_omitting_the_required_section():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "implementation-only")
    assert "must produce:** none" in gen.render_body(demo)


# --- the branch invariant (Codex review) --------------------------------
#
# `--check` used to ask only whether a branch was behind the base, and
# whether the patch still applied to the local working tree. Neither is the
# invariant: a branch rebuilt on today's base but carrying an extra commit or
# a hand-edited diff answers "0 behind" and reports healthy. These tests pin
# the tree-equality check that replaced it, using throwaway git indexes so
# nothing touches the working tree.


def _tree_of(base_ref: str, *, patch: Path | None = None, extra: tuple[str, str] | None = None) -> str:
    """Build `base_ref + patch (+ extra file)` and return its tree hash."""
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def run(*args, **kw):
            return subprocess.run(["git", *args], cwd=gen.REPO_ROOT, text=True,
                                  capture_output=True, env=env, **kw)

        run("read-tree", f"{base_ref}^{{tree}}")
        if patch is not None:
            run("apply", "--cached", str(patch))
        if extra is not None:
            name, content = extra
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                                  cwd=gen.REPO_ROOT, input=content, text=True,
                                  capture_output=True).stdout.strip()
            run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        return run("write-tree").stdout.strip()


def test_expected_tree_is_base_plus_patch():
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    assert gen.expected_tree(demo, "origin/main") == _tree_of(
        "origin/main", patch=REPO_ROOT / demo["patch"]
    )


def test_expected_tree_differs_from_the_untouched_base():
    """Guards against a patch that silently applies to nothing."""
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    assert gen.expected_tree(demo, "origin/main") != _tree_of("origin/main")


def test_a_branch_with_an_extra_change_is_not_the_expected_tree():
    """Codex's case: current base, patch applied, plus something else."""
    demo = next(d for d in gen.load_manifest(MANIFEST) if d["id"] == "binary-abi-break")
    drifted = _tree_of("origin/main", patch=REPO_ROOT / demo["patch"],
                       extra=("EXTRA.txt", "an extra commit\n"))
    assert drifted != gen.expected_tree(demo, "origin/main")


def test_a_branch_carrying_a_different_patch_is_not_the_expected_tree():
    demos = gen.load_manifest(MANIFEST)
    mine = next(d for d in demos if d["id"] == "binary-abi-break")
    theirs = next(d for d in demos if d["id"] == "compatible-addition")
    assert gen.expected_tree(mine, "origin/main") != gen.expected_tree(theirs, "origin/main")


def test_expected_tree_leaves_the_working_tree_and_index_alone():
    import subprocess

    before = subprocess.run(["git", "status", "--porcelain"], cwd=gen.REPO_ROOT,
                            text=True, capture_output=True).stdout
    for demo in gen.load_manifest(MANIFEST):
        gen.expected_tree(demo, "origin/main")
    after = subprocess.run(["git", "status", "--porcelain"], cwd=gen.REPO_ROOT,
                           text=True, capture_output=True).stdout
    assert before == after


def test_check_reports_the_real_branches_as_drifted():
    """All five are stale today; the check must say so rather than pass."""
    problems = gen.check(gen.load_manifest(MANIFEST), "origin/main")
    assert problems
    for demo in gen.load_manifest(MANIFEST):
        assert any(demo["id"] in p for p in problems), demo["id"]


def test_check_distinguishes_behind_from_divergent():
    """Two different problems deserve two different messages."""
    problems = gen.check(gen.load_manifest(MANIFEST), "origin/main")
    assert any("commit(s) behind" in p for p in problems)
    assert any("is not origin/main +" in p for p in problems)


# --- detached HEAD (Codex review) ---------------------------------------


def test_write_restores_a_detached_head_to_its_original_commit(tmp_path):
    """`rev-parse --abbrev-ref HEAD` says "HEAD" when detached, so restoring
    with `git checkout HEAD` was a no-op that left the caller on the last
    generated branch. Exercised against a real throwaway repository."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args, **kw):
        return subprocess.run(["git", *args], cwd=repo, text=True,
                              capture_output=True, check=False, **kw)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "f.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-q", "-m", "one")
    detached_at = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "--detach", detached_at)
    assert run("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == ""

    patch = tmp_path / "p.patch"
    patch.write_text(
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1 +1 @@\n-one\n+two\n"
    )
    demo = {"id": "d", "branch": "test/d", "title": "t",
            "patch": str(patch), "expect": {}}

    original_root = gen.REPO_ROOT
    gen.REPO_ROOT = repo
    try:
        gen.write([demo], "main", None)
    finally:
        gen.REPO_ROOT = original_root

    assert run("rev-parse", "--verify", "test/d").returncode == 0, "branch was created"
    assert run("rev-parse", "HEAD").stdout.strip() == detached_at
    assert run("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == "", \
        "HEAD must still be detached, not sitting on the generated branch"


def test_write_restores_a_named_branch(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, text=True,
                              capture_output=True, check=False)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "f.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-q", "-m", "one")
    run("checkout", "-q", "-b", "my-work")

    patch = tmp_path / "p.patch"
    patch.write_text(
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1 +1 @@\n-one\n+two\n"
    )
    demo = {"id": "d", "branch": "test/d", "title": "t",
            "patch": str(patch), "expect": {}}

    original_root = gen.REPO_ROOT
    gen.REPO_ROOT = repo
    try:
        gen.write([demo], "main", None)
    finally:
        gen.REPO_ROOT = original_root

    assert run("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == "my-work"
