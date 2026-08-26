"""Tests for the clean-job facts-pack reuse harness (ci/run_plugin_pack_reuse.py).

`abicheck` itself is not invoked here: the property under test is the
harness's own judgement -- what it counts as a rejection, what it refuses to
count as agreement, and which mutations it can actually perform against a
real pack shape. `run_compare` is stubbed so each case can hand back the
exact outcome shape a real run would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_plugin_pack_reuse as reuse


def _pack(root: Path, *, manifest: dict | None = None, tus: int = 2) -> Path:
    pack = root / "abicheck_inputs"
    (pack / "source_facts").mkdir(parents=True)
    for index in range(tus):
        (pack / "source_facts" / f"tu{index}.jsonl").write_text(
            json.dumps({"kind": "decl", "name": f"f{index}"}) + "\n", encoding="utf-8"
        )
    (pack / "manifest.json").write_text(
        json.dumps(
            manifest
            if manifest is not None
            else {
                "abicheck_inputs_version": 1,
                "kind": "abicheck_inputs",
                "headers": ["include/lab/math.h"],
                "plugin_llvm_version": "18",
                "source_facts": ["source_facts"],
            }
        ),
        encoding="utf-8",
    )
    return pack


def _bundle(root: Path) -> Path:
    _pack(root)
    (root / "lib").mkdir()
    (root / "lib" / "libmath.so").write_bytes(b"\x7fELF")
    (root / "include" / "lab").mkdir(parents=True)
    (root / "include" / "lab" / "math.h").write_text("int f0();\n", encoding="utf-8")
    (root / "baseline").mkdir()
    (root / "baseline" / "math.base.abicheck.json").write_text("{}", encoding="utf-8")
    return root


# --- rejection channels -------------------------------------------------


def test_clean_l4_report_is_not_a_rejection():
    outcome = reuse.Outcome(exit_code=0, report={"verdict": "NO_CHANGE"}, depth_errors=[])
    assert outcome.rejection_channel() is None


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (reuse.Outcome(64, {"verdict": "NO_CHANGE"}, []), "operational-exit:64"),
        (reuse.Outcome(0, None, ["no report"]), "no-report"),
        (reuse.Outcome(0, {"operational_errors": ["boom"]}, []), "operational-errors"),
        (reuse.Outcome(0, {}, ["effective_depth='headers'"]), "depth-contract"),
    ],
)
def test_each_rejection_channel_is_recognized(outcome, expected):
    assert outcome.rejection_channel() == expected


def test_verdict_exit_codes_are_not_operational_failures():
    """A BREAKING verdict is a produced report, not a tool failure."""
    for code in (1, 2, 4):
        assert reuse.Outcome(code, {"verdict": "BREAKING"}, []).rejection_channel() is None


# --- clean workspace ----------------------------------------------------


def test_clean_workspace_accepts_a_harness_only_checkout(tmp_path):
    (tmp_path / "ci").mkdir()
    assert reuse.assert_clean_workspace(tmp_path) == []


@pytest.mark.parametrize("marker", ["src", "include", "bazel-bin", "MODULE.bazel"])
def test_clean_workspace_rejects_a_source_tree(tmp_path, marker):
    (tmp_path / marker).mkdir()
    errors = reuse.assert_clean_workspace(tmp_path)
    assert errors and marker in errors[0]


# --- agreement is not silence -------------------------------------------


def test_two_reports_naming_no_targets_do_not_agree(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "verdict": "COMPATIBLE",
        "analysis_assurance": {"depth_satisfied": True, "status": "complete",
                               "effective_depth": "source"},
        "diff": {"findings": []},
    }), encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(0, {
            "verdict": "NO_CHANGE",
            "analysis_assurance": {"depth_satisfied": True, "status": "complete",
                               "effective_depth": "source"},
            "changes": [],
        }, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    accounting = next(a for a in receipt["assertions"] if a["assertion"] == "same-target-accounting")
    assert not accounting["passed"]
    assert "equal silence" in accounting["errors"][0]


def test_missing_replay_report_fails_rather_than_skips(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(0, {"verdict": "NO_CHANGE", "changes": []}, []),
    )
    receipt = reuse.run(root, tmp_path / "absent.json", tmp_path / "work", tmp_path / "ws")
    for name in ("same-findings-as-replay", "same-effective-depth", "same-target-accounting"):
        item = next(a for a in receipt["assertions"] if a["assertion"] == name)
        assert not item["passed"]


def test_scan_and_compare_shapes_agree_on_kind_symbol(tmp_path, monkeypatch):
    """The replay side is scan-mode and carries no old/new values."""
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps(
            {
                "verdict": "BREAKING",
                "library": "libmath.so",
                "analysis_assurance": {"effective_depth": "source",
                                       "depth_satisfied": True, "status": "complete"},
                "diff": {"findings": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(
            2,
            {
                "verdict": "BREAKING",
                "library": "libmath.so",
                "analysis_assurance": {"effective_depth": "source",
                                       "depth_satisfied": True, "status": "complete"},
                "changes": [
                    {"kind": "SYMBOL_REMOVED", "symbol": "f0", "old_value": "int f0()", "new_value": ""}
                ],
            },
            [],
        ),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    for name in ("same-findings-as-replay", "same-effective-depth", "same-target-accounting"):
        item = next(a for a in receipt["assertions"] if a["assertion"] == name)
        assert item["passed"], item["errors"]


def test_diverging_findings_are_reported_on_both_sides(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(
        json.dumps({"verdict": "BREAKING", "library": "libmath.so",
                    "analysis_assurance": {"depth_satisfied": True, "status": "complete",
                               "effective_depth": "source"},
                    "diff": {"findings": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(
            2, {"verdict": "BREAKING", "library": "libmath.so",
                "changes": [{"kind": "SYMBOL_REMOVED", "symbol": "f1"}]}, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    item = next(a for a in receipt["assertions"] if a["assertion"] == "same-findings-as-replay")
    assert not item["passed"]
    assert "f0" in item["errors"][0] and "f1" in item["errors"][0]


# --- pack mutations -----------------------------------------------------


def test_missing_tu_removes_exactly_one_facts_file(tmp_path):
    pack = _pack(tmp_path, tus=3)
    reuse.mutate_missing_tu(pack)
    assert len(list((pack / "source_facts").glob("*.jsonl"))) == 2


def test_corrupt_pack_makes_a_facts_file_unparseable(tmp_path):
    pack = _pack(tmp_path)
    reuse.mutate_corrupt_pack(pack)
    corrupted = [
        p for p in (pack / "source_facts").glob("*.jsonl")
        if not p.read_text().strip().startswith("{\"")
    ]
    assert corrupted


def test_wrong_llvm_major_bumps_the_recorded_version(tmp_path):
    pack = _pack(tmp_path)
    detail = reuse.mutate_wrong_llvm_major(pack)
    manifest = json.loads((pack / "manifest.json").read_text())
    assert manifest["plugin_llvm_version"] != "18"
    assert "plugin_llvm_version" in detail


def test_unversioned_pack_is_a_failure_not_a_skip(tmp_path):
    """A pack with no producer identity cannot be rejected for a wrong one."""
    pack = _pack(tmp_path, manifest={"kind": "abicheck_inputs", "headers": ["a.h"]})
    with pytest.raises(reuse.ScenarioError) as exc:
        reuse.mutate_wrong_llvm_major(pack)
    assert "no populated plugin/LLVM identity" in str(exc.value)


def test_the_real_upstream_manifest_shape_is_reported_not_silently_passed(tmp_path):
    """Codex review: the exact manifest merge_abicheck_facts.py emits.

    It carries `abicheck_inputs_version` (the pack FORMAT version) and an
    empty `version`, and no LLVM identity at all. A substring search on
    "version" picked up the format key, so the case bumped a schema version
    and any rejection proved schema validation rather than the LLVM binding
    this assertion claims to test.
    """
    pack = _pack(tmp_path, manifest={
        "abicheck_inputs_version": 1, "binary": "", "compile_db": "",
        "created_by": "abicheck-integration-lab merge_abicheck_facts.py",
        "exported_symbols": [], "headers": [], "kind": "abicheck_inputs",
        "library": "", "source_facts": ["source_facts"], "version": "",
    })
    with pytest.raises(reuse.ScenarioError):
        reuse.mutate_wrong_llvm_major(pack)
    # And the format version must be left exactly as it was.
    assert json.loads((pack / "manifest.json").read_text())["abicheck_inputs_version"] == 1


def test_the_pack_format_version_is_never_what_gets_mutated(tmp_path):
    pack = _pack(tmp_path, manifest={
        "abicheck_inputs_version": 1, "version": "", "kind": "abicheck_inputs",
        "plugin_llvm_version": "18", "headers": ["a.h"],
    })
    detail = reuse.mutate_wrong_llvm_major(pack)
    manifest = json.loads((pack / "manifest.json").read_text())
    assert manifest["abicheck_inputs_version"] == 1
    assert manifest["version"] == ""
    assert manifest["kind"] == "abicheck_inputs"
    assert manifest["plugin_llvm_version"] != "18"
    assert detail == "bumped LLVM/plugin version in plugin_llvm_version"


def test_an_empty_producer_identity_does_not_count_as_mutated(tmp_path):
    """An empty identity is no identity -- it cannot be made "wrong"."""
    pack = _pack(tmp_path, manifest={"kind": "abicheck_inputs", "headers": ["a.h"],
                                     "clang_version": "   "})
    with pytest.raises(reuse.ScenarioError):
        reuse.mutate_wrong_llvm_major(pack)


@pytest.mark.parametrize("key", ["llvm_major", "clang_version", "compiler_id", "plugin_build"])
def test_real_producer_identity_keys_are_found(tmp_path, key):
    pack = _pack(tmp_path, manifest={"kind": "abicheck_inputs", "headers": ["a.h"], key: "18"})
    assert key in reuse.mutate_wrong_llvm_major(pack)


def test_empty_public_roots_empties_the_declared_header_list(tmp_path):
    pack = _pack(tmp_path)
    reuse.mutate_empty_public_roots(pack)
    assert json.loads((pack / "manifest.json").read_text())["headers"] == []


def test_pack_with_no_public_root_key_is_a_failure_not_a_skip(tmp_path):
    pack = _pack(tmp_path, manifest={"kind": "abicheck_inputs", "plugin_llvm_version": "18"})
    with pytest.raises(reuse.ScenarioError):
        reuse.mutate_empty_public_roots(pack)


def test_a_harness_error_in_a_negative_case_fails_that_assertion(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    _pack(root, manifest={"kind": "abicheck_inputs"})
    (root / "lib").mkdir()
    (root / "lib" / "libmath.so").write_bytes(b"\x7fELF")
    (root / "include").mkdir()
    (root / "include" / "math.h").write_text("int f0();\n", encoding="utf-8")
    (root / "baseline").mkdir()
    (root / "baseline" / "b.json").write_text("{}", encoding="utf-8")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"library": "libmath.so", "diff": {"findings": []}}), encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(0, {"verdict": "NO_CHANGE", "library": "libmath.so", "changes": []}, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    item = next(a for a in receipt["assertions"] if a["assertion"] == "rejects/wrong-llvm-major")
    assert not item["passed"]
    assert "no populated plugin/LLVM identity" in item["errors"][0]


# --- negative cases actually run against a copy --------------------------


def test_negative_cases_never_mutate_the_shipped_bundle(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    before = {
        p.relative_to(root): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"library": "libmath.so", "diff": {"findings": []}}), encoding="utf-8")
    monkeypatch.setattr(reuse, "run_compare", lambda **kw: reuse.Outcome(64, None, ["x"]))
    reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    after = {
        p.relative_to(root): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }
    assert before == after


def test_all_six_negative_cases_are_exercised(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"library": "libmath.so", "diff": {"findings": []}}), encoding="utf-8")
    monkeypatch.setattr(reuse, "run_compare", lambda **kw: reuse.Outcome(64, None, ["x"]))
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    assert {c["case"] for c in receipt["negative_cases"]} == {
        "pack-removed", "stale-source-digest", "missing-tu",
        "wrong-llvm-major", "empty-public-roots", "corrupted-pack",
    }
    assert all(c["rejected"] for c in receipt["negative_cases"])


def test_a_pack_that_survives_every_mutation_fails_the_scenario(tmp_path, monkeypatch):
    """The whole point: silently accepting broken evidence is the failure."""
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"library": "libmath.so", "diff": {"findings": []}}), encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(0, {"verdict": "NO_CHANGE", "library": "libmath.so", "changes": []}, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    assert not receipt["passed"]
    failed = {a["assertion"] for a in receipt["assertions"] if not a["passed"]}
    assert "rejects/pack-removed" in failed
    assert "rejects/stale-source-digest" in failed


def test_stale_header_case_edits_a_copy_of_the_header(tmp_path):
    root = _bundle(tmp_path / "bundle")
    bundle = reuse.Bundle.load(root)
    seen = {}
    original = reuse.run_compare

    def spy(**kwargs):
        seen.update(kwargs)
        return reuse.Outcome(64, None, ["x"])

    reuse.run_compare = spy
    try:
        result = reuse.run_negative_case(
            "stale-source-digest", "d", None, True, bundle=bundle, workdir=tmp_path / "w"
        )
    finally:
        reuse.run_compare = original
    assert result["rejected"]
    assert seen["header"] != bundle.header
    assert "edited after" in Path(seen["header"]).read_text()
    assert "edited after" not in bundle.header.read_text()


def test_pack_removed_case_supplies_no_build_info(tmp_path):
    root = _bundle(tmp_path / "bundle")
    bundle = reuse.Bundle.load(root)
    seen = {}
    original = reuse.run_compare

    def spy(**kwargs):
        seen.update(kwargs)
        return reuse.Outcome(64, None, ["x"])

    reuse.run_compare = spy
    try:
        reuse.run_negative_case("pack-removed", "d", None, False, bundle=bundle, workdir=tmp_path / "w")
    finally:
        reuse.run_compare = original
    assert seen["pack"] is None


# --- bundle loading -----------------------------------------------------


def test_bundle_requires_exactly_one_binary(tmp_path):
    root = _bundle(tmp_path / "bundle")
    (root / "lib" / "libother.so").write_bytes(b"\x7fELF")
    with pytest.raises(reuse.ScenarioError) as exc:
        reuse.Bundle.load(root)
    assert "exactly one" in str(exc.value)


def test_bundle_requires_a_pack(tmp_path):
    root = _bundle(tmp_path / "bundle")
    import shutil

    shutil.rmtree(root / "abicheck_inputs")
    with pytest.raises(reuse.ScenarioError):
        reuse.Bundle.load(root)


# --- the reference must be sound (Codex review) --------------------------
#
# The replay leg runs `continue-on-error` and uploads its report
# unconditionally, so a report carrying operational errors or degraded
# assurance still reaches this harness. Accepting any JSON object let an
# INCOMPLETE replay satisfy all three equality assertions -- the reuse path
# "agreeing" with a reference that itself failed. Same trap as equal silence
# reading as equal accounting, one level up.


def test_a_clean_replay_report_is_a_usable_reference():
    assert reuse.replay_reference_problems(
        {"verdict": "COMPATIBLE",
         "analysis_assurance": {"depth_satisfied": True, "status": "complete"}}
    ) == []


def test_a_replay_report_with_operational_errors_is_rejected():
    problems = reuse.replay_reference_problems({"operational_errors": ["castxml exploded"]})
    assert problems and "operational error" in problems[0]


def test_a_replay_report_with_partial_assurance_is_rejected():
    problems = reuse.replay_reference_problems(
        {"analysis_assurance": {"depth_satisfied": True, "status": "partial",
                                "notes": ["public surface unresolved"]}}
    )
    status_problem = next(p for p in problems if "status=" in p)
    assert "expected 'complete'" in status_problem
    assert "public surface unresolved" in status_problem, "the note explains why"


def test_a_replay_report_denying_depth_is_rejected():
    problems = reuse.replay_reference_problems({"analysis_assurance": {"depth_satisfied": False}})
    assert problems and "depth_satisfied=False" in problems[0]


def test_a_report_without_an_assurance_block_is_rejected():
    """Codex review, second pass -- this test previously asserted the OPPOSITE,
    on my unverified assumption that a scan-mode report legitimately carries no
    assurance block.

    The exemption was the hole: effective_depth() falls back to `level.depth`,
    so a partial replay reporting `level.depth: source` with matching findings
    and targets passed every assertion while proving no completeness at all.
    ci/validate_source_depth.py already settles this for the repository -- a
    missing block is "source-depth satisfaction is unproven" and an error --
    so the exemption also contradicted an established convention.
    """
    problems = reuse.replay_reference_problems({"verdict": "COMPATIBLE", "diff": {"findings": []}})
    assert problems and "no analysis_assurance" in problems[0]


def test_a_partial_report_claiming_source_depth_via_level_is_rejected():
    """The concrete case: `level.depth: source` and nothing else."""
    problems = reuse.replay_reference_problems(
        {"verdict": "COMPATIBLE", "level": {"depth": "source"}, "diff": {"findings": []}}
    )
    assert problems, "level.depth alone is not a completeness proof"


def test_an_incomplete_replay_fails_the_three_agreement_assertions(tmp_path, monkeypatch):
    """The whole point: identical findings must NOT read as agreement when the
    reference itself errored."""
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "verdict": "BREAKING", "library": "libmath.so",
        "operational_errors": ["header parse failed"],
        "analysis_assurance": {"effective_depth": "source",
                                       "depth_satisfied": True, "status": "complete"},
        "diff": {"findings": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}]},
    }), encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(2, {
            "verdict": "BREAKING", "library": "libmath.so",
            "analysis_assurance": {"effective_depth": "source",
                                       "depth_satisfied": True, "status": "complete"},
            "changes": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}],
        }, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    sound = next(a for a in receipt["assertions"] if a["assertion"] == "replay-reference-is-sound")
    assert not sound["passed"]
    for name in ("same-findings-as-replay", "same-effective-depth", "same-target-accounting"):
        item = next(a for a in receipt["assertions"] if a["assertion"] == name)
        assert not item["passed"], name
        # The message must name the real reason, not claim a mismatch that
        # was never measured.
        assert "replay reference unusable" in item["errors"][0]
        assert "operational error" in item["errors"][0]


def test_a_sound_replay_still_lets_the_agreement_assertions_pass(tmp_path, monkeypatch):
    """The guard must not reject every real reference."""
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "verdict": "BREAKING", "library": "libmath.so",
        "analysis_assurance": {"effective_depth": "source", "status": "complete",
                               "depth_satisfied": True},
        "diff": {"findings": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}]},
    }), encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(2, {
            "verdict": "BREAKING", "library": "libmath.so",
            "analysis_assurance": {"effective_depth": "source", "status": "complete",
                               "depth_satisfied": True},
            "changes": [{"kind": "SYMBOL_REMOVED", "symbol": "f0"}],
        }, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    for name in ("replay-reference-is-sound", "same-findings-as-replay",
                 "same-effective-depth", "same-target-accounting"):
        item = next(a for a in receipt["assertions"] if a["assertion"] == name)
        assert item["passed"], (name, item["errors"])


def test_a_corrupt_replay_file_is_reported_not_crashed_on(tmp_path, monkeypatch):
    root = _bundle(tmp_path / "bundle")
    replay = tmp_path / "replay.json"
    replay.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(
        reuse, "run_compare",
        lambda **kw: reuse.Outcome(0, {"verdict": "NO_CHANGE", "changes": []}, []),
    )
    receipt = reuse.run(root, replay, tmp_path / "work", tmp_path / "ws")
    sound = next(a for a in receipt["assertions"] if a["assertion"] == "replay-reference-is-sound")
    assert not sound["passed"]


def test_an_empty_assurance_block_is_rejected():
    """Codex review, third pass on this validator. Requiring the block but
    rejecting only EXPLICIT negatives let `analysis_assurance: {}` through:
    depth_satisfied is None (not False) and status is None (not
    != "complete"). With `level.depth: source` supplying effective_depth() by
    fallback, every agreement assertion then succeeded on a reference that
    proved nothing.
    """
    problems = reuse.replay_reference_problems(
        {"analysis_assurance": {}, "level": {"depth": "source"}}
    )
    assert any("depth_satisfied=None" in p for p in problems)
    assert any("status=None" in p for p in problems)


@pytest.mark.parametrize("assurance", [
    {"status": "complete"},                       # depth_satisfied absent
    {"depth_satisfied": True},                    # status absent
    {"depth_satisfied": False, "status": "complete"},
    {"depth_satisfied": True, "status": "partial"},
])
def test_half_an_assurance_is_not_an_assurance(assurance):
    """Both fields must be affirmatively right; absent is not satisfied."""
    assert reuse.replay_reference_problems({"analysis_assurance": assurance})


def test_both_fields_affirmative_is_accepted():
    """The guard must still admit a genuinely sound reference."""
    assert reuse.replay_reference_problems(
        {"analysis_assurance": {"depth_satisfied": True, "status": "complete"}}
    ) == []
