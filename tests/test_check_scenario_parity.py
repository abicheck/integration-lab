"""Tests for scripts/check_scenario_parity.py.

Running a scenario under three build systems and checking each against its
own oracle is not parity: all three can satisfy their own oracle while
disagreeing with each other. These cover the comparison that catches that.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_scenario_parity as parity

REPO_ROOT = Path(__file__).resolve().parent.parent


def _report(verdict="BREAKING", changes=(), suppressed=()) -> dict:
    return {
        "verdict": verdict,
        "changes": [
            {"kind": k, "symbol": s, "severity": v} for k, s, v in changes
        ],
        # The REAL shape ABICheck emits: nested under "suppression", which is
        # where scripts/run_scenario.py's own oracle reads it. Using the
        # top-level shape here is what let a broken lookup pass its tests.
        "suppression": {
            "file_provided": True,
            "suppressed_count": len(suppressed),
            # Full finding identity, as ABICheck emits and as
            # normalized_suppressed now compares (Codex review): a
            # symbol-only fixture cannot exercise a kind/severity
            # disagreement, so it could not have caught that bug.
            "suppressed_changes": [
                {"kind": "func_removed", "symbol": s, "severity": "breaking"}
                for s in suppressed
            ],
        },
        # Provenance that legitimately differs per build system and must
        # never make parity fail.
        "old_path": "/some/build/dir/libimpl.so",
        "elapsed_ms": 1234,
    }


BREAK = (("func_removed", "_Z3foov", "breaking"),)


def test_identical_reports_agree():
    results = {"cmake": {"s": _report(changes=BREAK)}, "make": {"s": _report(changes=BREAK)}}
    assert parity.compare(results) == []


def test_provenance_differences_do_not_break_parity():
    """Paths and timings differ by build system by definition."""
    a = _report(changes=BREAK)
    b = _report(changes=BREAK)
    b["old_path"] = "/a/completely/different/path/libimpl.so"
    b["elapsed_ms"] = 99999
    assert parity.compare({"cmake": {"s": a}, "make": {"s": b}}) == []


def test_a_finding_present_in_only_one_build_system_fails():
    """The real case this found: CMake emitted a SONAME and Make did not."""
    extra = BREAK + (("soname_bump_recommended", "DT_SONAME", "compatible"),)
    results = {"cmake": {"s": _report(changes=extra)}, "make": {"s": _report(changes=BREAK)}}
    errors = parity.compare(results)
    assert any("findings differ" in e for e in errors)
    assert any("soname_bump_recommended" in e for e in errors)


def test_differing_verdicts_fail():
    results = {
        "cmake": {"s": _report(verdict="BREAKING", changes=BREAK)},
        "make": {"s": _report(verdict="COMPATIBLE", changes=BREAK)},
    }
    assert any("verdict differs" in e for e in parity.compare(results))


def test_differing_severity_for_the_same_symbol_fails():
    results = {
        "cmake": {"s": _report(changes=(("func_removed", "_Z3foov", "breaking"),))},
        "make": {"s": _report(changes=(("func_removed", "_Z3foov", "compatible"),))},
    }
    assert any("findings differ" in e for e in parity.compare(results))


def test_differing_suppressed_symbols_fail():
    """Which symbol a suppression accepted is semantics, not provenance."""
    results = {
        "cmake": {"s": _report(changes=BREAK, suppressed=("_Z3bazv",))},
        "make": {"s": _report(changes=BREAK, suppressed=("_Z3quxv",))},
    }
    assert any("suppressed symbols differ" in e for e in parity.compare(results))


def test_a_single_build_system_cannot_claim_parity():
    errors = parity.compare({"cmake": {"s": _report(changes=BREAK)}})
    assert any("at least two build systems" in e for e in errors)


def test_no_overlapping_scenario_is_a_failure():
    """Otherwise a suite where nothing overlaps reports parity vacuously."""
    results = {"cmake": {"only_cmake": _report()}, "make": {"only_make": _report()}}
    assert any("nothing was compared" in e for e in parity.compare(results))


def test_a_scenario_declared_for_one_build_system_only_is_not_an_error():
    """build-matrix.yaml legitimately declares partial coverage."""
    results = {
        "cmake": {"shared": _report(changes=BREAK), "cmake_only": _report()},
        "make": {"shared": _report(changes=BREAK)},
    }
    assert parity.compare(results) == []


def test_empty_results_are_a_failure():
    assert parity.compare({"cmake": {}, "make": {}})


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_load_results_skips_bookkeeping_files(tmp_path: Path):
    (tmp_path / "a.clang.json").write_text(json.dumps(_report()), encoding="utf-8")
    (tmp_path / "summary.json").write_text("[]", encoding="utf-8")
    (tmp_path / "skipped.json").write_text("{}", encoding="utf-8")
    loaded = parity.load_results(tmp_path)
    assert set(loaded) == {"a.clang"}


def test_unreadable_report_is_an_error(tmp_path: Path):
    (tmp_path / "a.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(parity.ParityError, match="unreadable report"):
        parity.load_results(tmp_path)


def test_missing_directory_is_an_error(tmp_path: Path):
    with pytest.raises(parity.ParityError, match="not a results directory"):
        parity.load_results(tmp_path / "nope")


def test_cli_writes_a_receipt_and_fails_closed(tmp_path: Path):
    for name, changes in (("cmake", BREAK), ("make", ())):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "s.json").write_text(json.dumps(_report(changes=changes)), encoding="utf-8")
    out = tmp_path / "parity.json"
    code = parity.main([
        "--results", f"cmake={tmp_path/'cmake'}", f"make={tmp_path/'make'}",
        "--out", str(out),
    ])
    assert code == 1
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["ok"] is False
    assert receipt["build_systems"] == ["cmake", "make"]
    assert receipt["errors"]


def test_cli_bad_results_argument():
    assert parity.main(["--results", "not-a-pair"]) == 1


# --------------------------------------------------------------------------
# The declared matrix
# --------------------------------------------------------------------------


def test_cmake_and_make_declare_the_same_scenarios():
    """Parity needs overlap; divergent matrices would silently shrink it."""
    import yaml

    matrix = yaml.safe_load(
        (REPO_ROOT / "scenarios" / "build-matrix.yaml").read_text(encoding="utf-8")
    )["build_systems"]
    assert set(matrix["cmake"]) == set(matrix["make"])
    assert len(matrix["cmake"]) >= 5


def test_fixture_recipes_match_the_bazel_reference_shape():
    """No recipe may emit a SONAME the Bazel reference does not.

    Bazel's `cc_binary(linkshared = True)` emits no DT_SONAME, so the
    oracle's expected_gating_symbols holds only API symbols. A recipe that
    adds one makes ABICheck report an extra soname_bump_recommended
    [DT_SONAME] finding, which fails suppression_partial under that build
    system while it passes under Bazel -- an artifact-shape difference, not
    a semantic one.
    """
    makefile = (REPO_ROOT / "buildsystems" / "make" / "fixtures" / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "-Wl,-soname" not in makefile.replace("# ", "").split("all:")[-1] or True
    # The recipe line itself must not pass -soname.
    recipe = [line for line in makefile.splitlines() if line.startswith("\t")]
    assert recipe, "no recipe lines found"
    assert not any("-Wl,-soname" in line for line in recipe), recipe

    cmake = (REPO_ROOT / "buildsystems" / "cmake" / "fixtures" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )
    assert "NO_SONAME ON" in cmake


# --------------------------------------------------------------------------
# Codex review: the suppression lookup and operational-error gating
# --------------------------------------------------------------------------


def test_suppressed_symbols_are_read_from_the_real_report_shape():
    """Regression guard: the top-level lookup returned nothing, always.

    That made the suppression-parity assertion compare two empty sets --
    dead code wearing the shape of a check.
    """
    report = _report(changes=BREAK, suppressed=("_ZN8scenario13legacy_metricEi",))
    assert parity.normalized_suppressed(report) == {
        ("func_removed", "_ZN8scenario13legacy_metricEi", "breaking")
    }
    assert "suppressed_changes" not in report, "the real shape nests this"


def test_suppression_parity_is_not_vacuous():
    """Two reports suppressing DIFFERENT symbols must disagree."""
    a = _report(changes=BREAK, suppressed=("_ZN8scenario13legacy_metricEi",))
    b = _report(changes=BREAK, suppressed=("_ZN8scenario12required_apiEi",))
    errors = parity.compare({"cmake": {"s": a}, "make": {"s": b}})
    assert any("suppressed symbols differ" in e for e in errors)
    assert any("legacy_metric" in e for e in errors)


def test_top_level_suppression_shape_still_works_for_fixtures():
    """A hand-written fixture using the flat shape is still understood."""
    report = {"verdict": "BREAKING", "suppressed_changes": [
        {"kind": "func_removed", "symbol": "_Z3foov", "severity": "breaking"}]}
    assert parity.normalized_suppressed(report) == {
        ("func_removed", "_Z3foov", "breaking")
    }


def test_report_with_operational_errors_is_rejected():
    """Incomplete evidence is not comparable, even when it matches."""
    clean = _report(changes=BREAK)
    broken = _report(changes=BREAK)
    broken["operational_errors"] = [{"code": "castxml_failed"}]
    errors = parity.compare({"cmake": {"s": clean}, "make": {"s": broken}})
    assert any("operational error" in e for e in errors)
    assert any("castxml_failed" in e for e in errors)


def test_operational_errors_matter_most_for_empty_finding_sets():
    """A clean NO_CHANGE and an incomplete run look identical otherwise."""
    clean = _report(verdict="NO_CHANGE")
    broken = _report(verdict="NO_CHANGE")
    broken["operational_errors"] = ["dwarf extraction failed"]
    assert parity.compare({"cmake": {"s": clean}, "make": {"s": broken}})


def test_per_library_operational_errors_are_collected():
    report = {"libraries": [{"operational_errors": ["nested"]}], "operational_errors": ["top"]}
    assert len(parity.operational_errors(report)) == 2


def test_an_incomplete_scenario_still_counts_as_compared():
    """Otherwise it would also trip the "nothing was compared" guard and
    report two different problems for one cause."""
    clean = _report(changes=BREAK)
    broken = _report(changes=BREAK)
    broken["operational_errors"] = ["boom"]
    errors = parity.compare({"cmake": {"s": clean}, "make": {"s": broken}})
    assert not any("nothing was compared" in e for e in errors)


# --------------------------------------------------------------------------
# Codex review: coverage must not be able to shrink silently
# --------------------------------------------------------------------------


DECLARED = {"cmake": {"add_function", "remove_function"}, "make": {"add_function", "remove_function"}}


#: Every declared scenario runs under both header frontends, matching the
#: real scenarios/manifest.yaml (all seven use the `expected: {castxml, clang}`
#: form).
PROFILES = {name: {"castxml", "clang"} for names in DECLARED.values() for name in names}


def _complete(build_systems=None):
    """Every declared cell present, for the given build systems."""
    return {
        bs: {stem: _report()
             for name in names
             for stem in parity.expected_stems(name, PROFILES.get(name, set()))}
        for bs, names in DECLARED.items()
        if build_systems is None or bs in build_systems
    }


def test_every_declared_scenario_must_report():
    """run_scenario.py treats an absent mapping as a SKIP, so deleting one
    would otherwise remove a scenario from parity while the gate stayed
    green."""
    results = _complete()
    del results["make"]["remove_function.castxml"]
    del results["make"]["remove_function.clang"]
    errors = parity.missing_declared_reports(results, DECLARED, PROFILES)
    assert any("remove_function" in e and "make" in e for e in errors)
    assert any("coverage shrank silently" in e for e in errors)


def test_full_declared_coverage_passes():
    assert parity.missing_declared_reports(_complete(), DECLARED, PROFILES) == []


def test_one_missing_frontend_is_a_shrink():
    """Codex review, second pass. The check collapsed a stem at its first
    dot, so `add_function.castxml` alone satisfied `add_function` and the
    missing Clang half passed -- while compare() went on comparing the Clang
    reports that survived elsewhere. One build-system/frontend CELL could
    vanish with the gate still green.
    """
    results = _complete()
    del results["cmake"]["add_function.clang"]
    errors = parity.missing_declared_reports(results, DECLARED, PROFILES)
    assert any("add_function.clang" in e and "cmake" in e for e in errors)


def test_the_other_frontend_of_the_same_scenario_is_not_reported_missing():
    """Only the absent cell is named, so the message stays actionable."""
    results = _complete()
    del results["cmake"]["add_function.clang"]
    errors = parity.missing_declared_reports(results, DECLARED, PROFILES)
    assert not any("add_function.castxml" in e for e in errors)


def test_a_single_run_scenario_expects_a_bare_stem():
    """The older `expected_verdict:` form runs once under the default
    frontend, and its report carries no profile suffix."""
    assert parity.expected_stems("solo", set()) == {"solo"}
    results = {"cmake": {"solo": _report()}}
    assert parity.missing_declared_reports(results, {"cmake": {"solo"}}, {"solo": set()}) == []


def test_a_wholly_missing_build_system_is_a_shrink_by_default():
    """Codex review, second pass -- this test previously asserted the OPPOSITE.

    Exempting an absent build system was true for a deliberate local run and
    false for the case that matters: dropping the Make pair from the
    workflow's --results, or mistyping its name, removed an entire leg while
    the surviving legs still compared clean and the gate stayed green. That
    is a strictly bigger hole than the missing cells this function exists to
    catch.
    """
    errors = parity.missing_declared_reports(_complete(["cmake"]), DECLARED, PROFILES)
    assert any("make" in e and "entire build system" in e for e in errors)


def test_a_mistyped_build_system_name_is_caught():
    """The typo case: results arrive under a name nothing declares."""
    results = _complete()
    results["mak"] = results.pop("make")
    errors = parity.missing_declared_reports(results, DECLARED, PROFILES)
    assert any("make" in e and "entire build system" in e for e in errors)


def test_a_deliberate_partial_run_can_opt_out():
    """The escape hatch survives, but has to be asked for by name."""
    assert parity.missing_declared_reports(
        _complete(["cmake"]), DECLARED, PROFILES, allow_partial=True
    ) == []


def test_allow_partial_does_not_excuse_a_missing_cell():
    """Opting out of the whole-leg check must not also disable the cell check."""
    results = _complete(["cmake"])
    del results["cmake"]["add_function.clang"]
    errors = parity.missing_declared_reports(
        results, DECLARED, PROFILES, allow_partial=True
    )
    assert any("add_function.clang" in e for e in errors)


def test_the_workflow_supplies_every_declared_build_system():
    """The gate is only strict if CI actually passes all the legs."""
    import yaml as _yaml

    root = Path(__file__).resolve().parent.parent
    document = _yaml.safe_load(
        (root / ".github/workflows/integration-shadow.yml").read_text(encoding="utf-8")
    )
    step = next(
        s for s in document["jobs"]["scenario_parity"]["steps"]
        if isinstance(s.get("run"), str) and "check_scenario_parity.py" in s["run"]
    )
    declared = parity.load_declared(root / "scenarios" / "build-matrix.yaml")
    for build_system in declared:
        assert f"{build_system}=" in step["run"], build_system
    assert "--allow-partial" not in step["run"], "CI must not opt out of the strict check"


def test_scenario_profiles_reads_the_real_manifest():
    """The profiles come from the real manifest, not a hardcoded pair."""
    root = Path(__file__).resolve().parent.parent
    profiles = parity.scenario_profiles(root / "scenarios" / "manifest.yaml")
    assert profiles, "the manifest declares scenarios"
    for name, declared_profiles in profiles.items():
        assert declared_profiles == {"castxml", "clang"}, name


def test_the_real_declared_matrix_has_no_missing_cells_when_complete():
    """Guards against the fix over-reporting on the actual suite."""
    root = Path(__file__).resolve().parent.parent
    profiles = parity.scenario_profiles(root / "scenarios" / "manifest.yaml")
    declared = parity.load_declared(root / "scenarios" / "build-matrix.yaml")
    declared.pop("bazel", None)
    results = {
        bs: {stem: _report()
             for name in names
             for stem in parity.expected_stems(name, profiles.get(name, set()))}
        for bs, names in declared.items()
    }
    assert parity.missing_declared_reports(results, declared, profiles) == []


def test_declared_matrix_is_loaded_from_the_real_file():
    declared = parity.load_declared(REPO_ROOT / "scenarios" / "build-matrix.yaml")
    assert set(declared) == {"cmake", "make"}
    assert declared["cmake"] == declared["make"]
    assert len(declared["cmake"]) >= 5


def test_missing_build_matrix_is_an_error(tmp_path: Path):
    with pytest.raises(parity.ParityError):
        parity.load_declared(tmp_path / "nope.yaml")


def test_empty_build_matrix_is_an_error(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text("build_systems: {}\n", encoding="utf-8")
    with pytest.raises(parity.ParityError, match="no build_systems"):
        parity.load_declared(path)


# --- suppressed findings keep full identity (Codex review) --------------


def _suppressing(kind: str, symbol: str, severity: str) -> dict:
    return {"suppression": {"suppressed_changes": [
        {"kind": kind, "symbol": symbol, "severity": severity}]}}


def test_same_symbol_different_suppressed_kind_is_a_disagreement():
    """The bug: projecting to the symbol alone made these equal, while
    unsuppressed findings are compared on (kind, symbol, severity)."""
    left = parity.normalized_suppressed(_suppressing("func_removed", "_Z1fv", "breaking"))
    right = parity.normalized_suppressed(_suppressing("param_type_changed", "_Z1fv", "breaking"))
    assert left != right


def test_same_symbol_different_suppressed_severity_is_a_disagreement():
    left = parity.normalized_suppressed(_suppressing("func_removed", "_Z1fv", "breaking"))
    right = parity.normalized_suppressed(_suppressing("func_removed", "_Z1fv", "risk"))
    assert left != right


def test_identical_suppressions_still_agree():
    """A tighter identity must not make every pair disagree."""
    left = parity.normalized_suppressed(_suppressing("func_removed", "_Z1fv", "breaking"))
    right = parity.normalized_suppressed(_suppressing("func_removed", "_Z1fv", "breaking"))
    assert left == right and left


def test_suppressed_identity_matches_the_unsuppressed_one():
    """Both sides of one suppression rule are the same objects; comparing
    them by different identities was the defect."""
    report = {
        "changes": [{"kind": "func_removed", "symbol": "_Z1fv", "severity": "breaking"}],
        "suppression": {"suppressed_changes": [
            {"kind": "func_removed", "symbol": "_Z1fv", "severity": "breaking"}]},
    }
    assert parity.normalized_findings(report) == parity.normalized_suppressed(report)


def test_non_dict_suppressed_entries_are_ignored():
    report = {"suppression": {"suppressed_changes": ["junk", None]}}
    assert parity.normalized_suppressed(report) == set()


# --- the Bazel reference leg (Codex review) -----------------------------


def test_dropping_the_reference_leg_is_caught():
    """build-matrix.yaml declares only cmake and make, so the declared-systems
    check could never require Bazel -- yet the fixtures are shaped to match
    it."""
    problems = parity.missing_reference_leg({"cmake": {}, "make": {}})
    assert problems and "reference build system" in problems[0]


def test_the_reference_leg_present_is_accepted():
    assert parity.missing_reference_leg({"bazel": {}, "cmake": {}, "make": {}}) == []


def test_allow_partial_exempts_the_reference_leg():
    assert parity.missing_reference_leg({"cmake": {}}, allow_partial=True) == []


def test_the_workflow_supplies_the_reference_leg():
    """The gate is only meaningful if CI actually passes Bazel."""
    import yaml as _yaml

    root = Path(__file__).resolve().parent.parent
    document = _yaml.safe_load(
        (root / ".github/workflows/integration-shadow.yml").read_text(encoding="utf-8")
    )
    step = next(
        s for s in document["jobs"]["scenario_parity"]["steps"]
        if isinstance(s.get("run"), str) and "check_scenario_parity.py" in s["run"]
    )
    assert f"{parity.REFERENCE_BUILD_SYSTEM}=" in step["run"]
