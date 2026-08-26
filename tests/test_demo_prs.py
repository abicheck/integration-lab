"""Tests for the generated demonstration PRs (roadmap item 14).

Two things are under test: the oracle's judgement of a gate report against a
declared expectation, and the generator's manifest/body handling. Git
operations are not exercised here -- `--check` reads real refs and is run
directly by an operator (see docs/operations.md); what these tests pin is
everything that decides whether a demonstration still demonstrates its claim.
"""

from __future__ import annotations

import contextlib
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
# the tree-equality check that replaced it.
#
# They build a throwaway repository rather than reading this one. An earlier
# revision asserted against `origin/main` and the real test/* branches; that
# passed locally, where those refs happen to be fetched, and failed in CI,
# where a PR checkout has no `refs/remotes/origin/main` at all -- seven tests
# depending on ambient state rather than on anything they set up. The real
# branches' actual drift is reported by the demo_branch_drift job, which is
# where a fact about the world belongs; a unit test asserting it would also
# have to be rewritten the moment someone regenerated them.


def _git(repo: Path, *args: str, **kw) -> str:
    import subprocess

    result = subprocess.run(["git", *args], cwd=repo, text=True,
                            capture_output=True, **kw)
    return result.stdout


def _repo_with_base(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with a `main`, an `origin/main` remote-tracking ref, and a
    patch that applies to it. Returns (repo, patch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # A remote-tracking ref without a remote, so `origin/main` resolves the
    # same way gen_demo_prs expects without any network.
    _git(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")

    patch = tmp_path / "p.patch"
    patch.write_text(
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1 +1 @@\n-one\n+two\n"
    )
    return repo, patch


def _demo_entry(patch: Path, branch: str = "test/d") -> dict:
    return {"id": "d", "branch": branch, "title": "t",
            "patch": str(patch), "expect": {}}


@contextlib.contextmanager
def _rooted_at(repo: Path):
    original = gen.REPO_ROOT
    gen.REPO_ROOT = repo
    try:
        yield
    finally:
        gen.REPO_ROOT = original


def test_expected_tree_is_base_plus_patch(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        want = gen.expected_tree(_demo_entry(patch), "origin/main")
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        got = gen.git("rev-parse", "test/d^{tree}").strip()
    assert got == want


def test_expected_tree_differs_from_the_untouched_base(tmp_path):
    """Guards against a patch that silently applies to nothing."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        want = gen.expected_tree(_demo_entry(patch), "origin/main")
        base = gen.git("rev-parse", "origin/main^{tree}").strip()
    assert want != base


def test_a_branch_with_an_extra_change_is_not_the_expected_tree(tmp_path):
    """Codex's case: current base, patch applied, plus something else."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        want = gen.expected_tree(_demo_entry(patch), "origin/main")
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        (repo / "EXTRA.txt").write_text("an extra commit\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        got = gen.git("rev-parse", "test/d^{tree}").strip()
    assert got != want


def test_a_branch_carrying_a_different_patch_is_not_the_expected_tree(tmp_path):
    repo, mine = _repo_with_base(tmp_path)
    theirs = tmp_path / "other.patch"
    theirs.write_text(
        "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1 +1 @@\n-one\n+three\n"
    )
    with _rooted_at(repo):
        assert gen.expected_tree(_demo_entry(mine), "origin/main") != gen.expected_tree(
            _demo_entry(theirs), "origin/main"
        )


def test_expected_tree_leaves_the_working_tree_and_index_alone(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        before = gen.git("status", "--porcelain")
        gen.expected_tree(_demo_entry(patch), "origin/main")
        after = gen.git("status", "--porcelain")
    assert before == after
    assert (repo / "f.txt").read_text() == "one\n", "the file itself is untouched"


def test_check_reports_a_drifted_branch(tmp_path):
    """A branch on the current base but carrying an extra change."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        (repo / "EXTRA.txt").write_text("extra\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("update-ref", "refs/remotes/origin/test/d", "refs/heads/test/d")
        gen.git("checkout", "-q", "main")
        problems = gen.check([_demo_entry(patch)], "origin/main")
    assert any("is not origin/main +" in p for p in problems), problems


def test_check_accepts_a_correctly_generated_branch(tmp_path):
    """The other direction: a gate that never passes is not a gate."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("update-ref", "refs/remotes/origin/test/d", "refs/heads/test/d")
        gen.git("checkout", "-q", "main")
        problems = gen.check([_demo_entry(patch)], "origin/main")
    assert problems == []


def test_check_reports_a_branch_that_is_behind(tmp_path):
    """"Behind" stays its own message -- far more actionable than a tree hash."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("update-ref", "refs/remotes/origin/test/d", "refs/heads/test/d")
        gen.git("checkout", "-q", "main")
        (repo / "moved-on.txt").write_text("main moved\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "main moves on")
        gen.git("update-ref", "refs/remotes/origin/main", "refs/heads/main")
        problems = gen.check([_demo_entry(patch)], "origin/main")
    assert any("commit(s) behind" in p for p in problems), problems


def test_check_reports_a_missing_branch(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        problems = gen.check([_demo_entry(patch, branch="test/absent")], "origin/main")
    assert any("does not exist on origin" in p for p in problems), problems


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


# --- --only applies to every mode (Codex review) ------------------------


def _two_demo_repo(tmp_path):
    """A repo where one demonstration is clean and another has drifted."""
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        # clean: base + patch, exactly
        gen.git("checkout", "-q", "-B", "test/clean", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("update-ref", "refs/remotes/origin/test/clean", "refs/heads/test/clean")
        # drifted: base + patch + something else
        gen.git("checkout", "-q", "-B", "test/drifted", "origin/main")
        gen.git("apply", str(patch))
        (repo / "EXTRA.txt").write_text("extra\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("update-ref", "refs/remotes/origin/test/drifted", "refs/heads/test/drifted")
        gen.git("checkout", "-q", "main")
    clean = {"id": "clean", "branch": "test/clean", "title": "t",
             "patch": str(patch), "expect": {}}
    drifted = {"id": "drifted", "branch": "test/drifted", "title": "t",
               "patch": str(patch), "expect": {}}
    return repo, [clean, drifted]


def test_check_on_one_demo_ignores_another_demos_drift(tmp_path):
    """`--check --only clean` must not fail because `drifted` drifted."""
    repo, demos = _two_demo_repo(tmp_path)
    with _rooted_at(repo):
        assert gen.check([d for d in demos if d["id"] == "clean"], "origin/main") == []
        # ...and the unfiltered call still sees the drift, so the filter is
        # what changed the answer rather than the check having gone blind.
        assert gen.check(demos, "origin/main")


def test_check_on_the_drifted_demo_still_reports_it(tmp_path):
    repo, demos = _two_demo_repo(tmp_path)
    with _rooted_at(repo):
        problems = gen.check([d for d in demos if d["id"] == "drifted"], "origin/main")
    assert any("drifted" in p for p in problems)


def test_only_with_an_unknown_id_is_an_error(tmp_path, capsys):
    """Previously a silent no-op that reported success for checking nothing."""
    code = gen.main(["--check", "--only", "no-such-demo", "--manifest", str(MANIFEST)])
    assert code == 2
    assert "no demonstration 'no-such-demo'" in capsys.readouterr().err


def test_only_names_the_known_ids_when_it_fails(tmp_path, capsys):
    gen.main(["--check", "--only", "typo", "--manifest", str(MANIFEST)])
    err = capsys.readouterr().err
    for demo in gen.load_manifest(MANIFEST):
        assert demo["id"] in err


# --- a failed restoration must not be silent (Codex review) -------------


def test_a_failed_restoration_is_reported(tmp_path):
    """`check=False` threw the result away, so the caller was left on a
    half-generated branch by the code that promises to put them back.

    Reachable: with a conflicting local edit `git checkout` refuses and HEAD
    stays put. Reproduced here rather than asserted about.
    """
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        # A branch whose content differs, checked out, then a conflicting
        # uncommitted edit -- so restoring to it will be refused.
        gen.git("checkout", "-q", "-b", "elsewhere")
        (repo / "f.txt").write_text("elsewhere\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "elsewhere")

        problem = None
        try:
            gen.git("checkout", "-q", "main")
            (repo / "f.txt").write_text("conflicting local edit\n")
            problem = gen._restore_head("elsewhere")
        finally:
            pass
    assert problem is not None, "the refused checkout must be reported"
    assert "could not restore the checkout" in problem
    assert "elsewhere" in problem


def test_a_successful_restoration_reports_nothing(tmp_path):
    """The guard must stay quiet on the normal path."""
    repo, _ = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-b", "side")
        gen.git("checkout", "-q", "main")
        assert gen._restore_head("side") is None
        assert gen.git("symbolic-ref", "--quiet", "--short", "HEAD").strip() == "side"


def test_restoration_names_where_the_checkout_was_actually_left(tmp_path):
    """The message has to say where the caller IS, not just where they wanted
    to be -- that is the actionable half."""
    repo, _ = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-b", "elsewhere")
        (repo / "f.txt").write_text("elsewhere\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "elsewhere")
        gen.git("checkout", "-q", "main")
        (repo / "f.txt").write_text("conflicting\n")
        problem = gen._restore_head("elsewhere")
    assert problem and "'main'" in problem


def test_a_detached_original_restores_by_detaching(tmp_path):
    repo, _ = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        sha = gen.git("rev-parse", "HEAD").strip()
        gen.git("checkout", "-q", "-b", "side")
        assert gen._restore_head(sha) is None
        assert gen.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() == ""
        assert gen.git("rev-parse", "HEAD").strip() == sha


# --- --push must validate what it is about to overwrite (Codex review) ----
#
# --force-with-lease detects a concurrent REMOTE update; it says nothing about
# whether the LOCAL content is the generated tree. check() cannot cover this:
# it inspects refs/remotes/origin/*, which is the state being replaced.


def test_local_drift_accepts_a_correctly_generated_branch(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("checkout", "-q", "main")
        assert gen.local_drift([_demo_entry(patch)], "origin/main") == []


def test_local_drift_catches_a_hand_edited_branch(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        (repo / "EXTRA.txt").write_text("hand edit\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("checkout", "-q", "main")
        problems = gen.local_drift([_demo_entry(patch)], "origin/main")
    assert any("is not origin/main plus" in p for p in problems), problems


def test_local_drift_reports_a_branch_that_was_never_written(tmp_path):
    repo, patch = _repo_with_base(tmp_path)
    with _rooted_at(repo):
        problems = gen.local_drift([_demo_entry(patch)], "origin/main")
    assert any("run --write first" in p for p in problems), problems


def test_push_refuses_before_pushing_anything(tmp_path, monkeypatch):
    """The refusal must come before the FIRST push: a force-push is not
    undoable, so validating per branch would leave the set half-replaced."""
    repo, patch = _repo_with_base(tmp_path)
    pushes: list[tuple] = []
    real_git = gen.git

    def _spy(*args, **kwargs):
        if args and args[0] == "push":
            pushes.append(args)
            return ""
        return real_git(*args, **kwargs)

    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        (repo / "EXTRA.txt").write_text("hand edit\n")
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("checkout", "-q", "main")
        monkeypatch.setattr(gen, "git", _spy)
        with pytest.raises(gen.DemoError) as excinfo:
            gen.push([_demo_entry(patch)], None, "origin/main")
    assert "refusing to force-push" in str(excinfo.value)
    assert pushes == []


def test_push_proceeds_when_the_local_branch_is_the_generated_tree(tmp_path, monkeypatch):
    """A guard that never lets anything through is not a guard."""
    repo, patch = _repo_with_base(tmp_path)
    pushes: list[tuple] = []
    real_git = gen.git

    def _spy(*args, **kwargs):
        if args and args[0] == "push":
            pushes.append(args)
            return ""
        return real_git(*args, **kwargs)

    with _rooted_at(repo):
        gen.git("checkout", "-q", "-B", "test/d", "origin/main")
        gen.git("apply", str(patch))
        gen.git("add", "-A")
        gen.git("commit", "-q", "-m", "t")
        gen.git("checkout", "-q", "main")
        monkeypatch.setattr(gen, "git", _spy)
        assert gen.push([_demo_entry(patch)], None, "origin/main") == ["test/d"]
    assert len(pushes) == 1
