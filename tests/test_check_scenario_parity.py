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
            "suppressed_changes": [{"symbol": s} for s in suppressed],
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


def test_make_fixture_recipe_sets_a_soname():
    """CMake and Bazel both set SONAME; a bare `g++ -shared` does not."""
    makefile = (REPO_ROOT / "buildsystems" / "make" / "fixtures" / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "-Wl,-soname,libimpl.so" in makefile


# --------------------------------------------------------------------------
# Codex review: the suppression lookup and operational-error gating
# --------------------------------------------------------------------------


def test_suppressed_symbols_are_read_from_the_real_report_shape():
    """Regression guard: the top-level lookup returned nothing, always.

    That made the suppression-parity assertion compare two empty sets --
    dead code wearing the shape of a check.
    """
    report = _report(changes=BREAK, suppressed=("_ZN8scenario13legacy_metricEi",))
    assert parity.normalized_suppressed(report) == {"_ZN8scenario13legacy_metricEi"}
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
    report = {"verdict": "BREAKING", "suppressed_changes": [{"symbol": "_Z3foov"}]}
    assert parity.normalized_suppressed(report) == {"_Z3foov"}


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
