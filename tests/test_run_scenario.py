"""Unit tests for scripts/run_scenario.py's PR3 item 2 additions
(--build-system cmake dispatch, scenarios/build-matrix.yaml loading).

Deliberately hermetic -- monkeypatches run_cmake_build()/
run_abicheck_compare() rather than actually invoking cmake/abicheck, since
this is a dispatch/plumbing test (does run_one() route to the right build
recipe and fail closed on an unmapped scenario), not a re-test of
ci/backends/cmake.py's own real-build coverage (tests/test_backends.py) or
of abicheck's own detection correctness (this repo's actual local/CI
`python3 scripts/run_scenario.py --build-system cmake` runs, which really
do shell out to cmake + the real abicheck CLI end-to-end).
"""

from __future__ import annotations

import pytest

from pathlib import Path

import run_scenario


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_load_build_matrix_reads_real_manifest():
    matrix = run_scenario.load_build_matrix(REPO_ROOT / "scenarios" / "build-matrix.yaml")
    assert "cmake" in matrix
    assert "add_function" in matrix["cmake"]
    assert "remove_function" in matrix["cmake"]
    assert matrix["cmake"]["add_function"]["old_fixture_dir"] == "fixtures/add_function/v1"


def test_load_build_matrix_missing_file_returns_empty_mapping(tmp_path):
    assert run_scenario.load_build_matrix(tmp_path / "does-not-exist.yaml") == {}


def test_run_one_skips_unmapped_scenario_under_cmake(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(run_scenario, "run_cmake_build", lambda *a, **kw: called.append(a))
    scenario = {"name": "change_signature", "expected_verdict": "BREAKING"}
    result = run_scenario.run_one(
        scenario, tmp_path, build_system="cmake", build_matrix={"cmake": {}}, scratch_dir=tmp_path
    )
    assert result is None
    assert called == []  # never attempted a build for an unmapped scenario


def test_run_one_dispatches_to_cmake_build_for_mapped_scenario(tmp_path, monkeypatch):
    build_calls = []

    def fake_cmake_build(fixture_dir, build_dir):
        build_calls.append((fixture_dir, build_dir))
        return build_dir / "libimpl.so"

    compare_calls = []

    def fake_compare(old_lib, new_lib, new_header, output_json, **kwargs):
        compare_calls.append((old_lib, new_lib))
        output_json.write_text('{"verdict": "COMPATIBLE"}')

    monkeypatch.setattr(run_scenario, "run_cmake_build", fake_cmake_build)
    monkeypatch.setattr(run_scenario, "run_abicheck_compare", fake_compare)

    scenario = {
        "name": "add_function",
        "expected_verdict": "COMPATIBLE",
        "new_header": "fixtures/add_function/v2/lib.h",
    }
    build_matrix = {
        "cmake": {
            "add_function": {
                "old_fixture_dir": "fixtures/add_function/v1",
                "new_fixture_dir": "fixtures/add_function/v2",
            }
        }
    }
    results = run_scenario.run_one(
        scenario, tmp_path, build_system="cmake", build_matrix=build_matrix, scratch_dir=tmp_path
    )
    assert len(build_calls) == 2  # old side, new side
    assert len(compare_calls) == 1
    assert results[0]["passed"] is True


def test_run_one_bazel_path_unchanged(tmp_path, monkeypatch):
    """The default --build-system bazel path must still read
    old_output/new_output straight from the scenario dict, exactly as
    before PR3 -- no build-matrix.yaml lookup involved at all.
    """
    monkeypatch.setattr(run_scenario, "run_bazel_build", lambda *a, **kw: None)

    compare_calls = []

    def fake_compare(old_lib, new_lib, new_header, output_json, **kwargs):
        compare_calls.append((str(old_lib), str(new_lib)))
        output_json.write_text('{"verdict": "COMPATIBLE"}')

    monkeypatch.setattr(run_scenario, "run_abicheck_compare", fake_compare)

    scenario = {
        "name": "add_function",
        "old_target": "//fixtures/add_function/v1:impl",
        "new_target": "//fixtures/add_function/v2:impl",
        "old_output": "bazel-bin/fixtures/add_function/v1/libimpl.so",
        "new_output": "bazel-bin/fixtures/add_function/v2/libimpl.so",
        "new_header": "fixtures/add_function/v2/lib.h",
        "expected_verdict": "COMPATIBLE",
    }
    results = run_scenario.run_one(scenario, tmp_path)
    assert len(compare_calls) == 1
    assert compare_calls[0][0].endswith("bazel-bin/fixtures/add_function/v1/libimpl.so")
    assert compare_calls[0][1].endswith("bazel-bin/fixtures/add_function/v2/libimpl.so")
    assert results[0]["passed"] is True


def test_run_one_dispatches_to_make_build_for_mapped_scenario(tmp_path, monkeypatch):
    """The third build system reaches its own builder, not cmake's."""
    import run_scenario

    calls = []
    monkeypatch.setattr(
        run_scenario, "run_make_build",
        lambda fixture_dir, build_dir: (calls.append((fixture_dir, build_dir)), tmp_path / "libimpl.so")[1],
    )
    monkeypatch.setattr(
        run_scenario, "run_one_profile",
        lambda scenario, old_lib, new_lib, profile, expected, results_dir: {"ok": True},
    )
    scenario = {"name": "add_function", "expected_verdict": "COMPATIBLE"}
    matrix = {"make": {"add_function": {
        "old_fixture_dir": "fixtures/add_function/v1",
        "new_fixture_dir": "fixtures/add_function/v2",
    }}}
    results = run_scenario.run_one(
        scenario, tmp_path, build_system="make", build_matrix=matrix, scratch_dir=tmp_path
    )
    assert results == [{"ok": True}]
    assert len(calls) == 2, calls


def test_builders_are_resolved_at_call_time(monkeypatch):
    """Binding the function object at import would ignore a replacement."""
    import run_scenario

    sentinel = object()
    monkeypatch.setattr(run_scenario, "run_make_build", sentinel)
    assert run_scenario._fixture_dir_builder("make") is sentinel


def test_unknown_build_system_names_the_known_ones(tmp_path):
    import run_scenario

    with pytest.raises(ValueError, match="bazel.*cmake.*make"):
        run_scenario.run_one(
            {"name": "x"}, tmp_path, build_system="ninja", build_matrix={}, scratch_dir=tmp_path
        )


def test_unmapped_scenario_is_skipped_not_substituted(tmp_path):
    """A missing mapping must return None (skipped), never fall back."""
    import run_scenario

    assert run_scenario.run_one(
        {"name": "not_in_matrix"}, tmp_path,
        build_system="make", build_matrix={"make": {}}, scratch_dir=tmp_path,
    ) is None


def test_bazel_build_is_unpinned_by_default(monkeypatch):
    """scenarios.yml installs plain gcc/g++, so pinning must be opt-in."""
    import run_scenario

    calls = []
    monkeypatch.setattr(run_scenario.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or type("P", (), {"returncode": 0})())
    run_scenario.run_bazel_build("//a", "//b")
    assert calls[0] == ["bazel", "build", "//a", "//b"]


def test_bazel_build_pins_the_producer_when_asked(monkeypatch):
    import run_scenario

    calls = []
    monkeypatch.setattr(run_scenario.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or type("P", (), {"returncode": 0})())
    run_scenario.run_bazel_build("//a", toolchain=("gcc-14", "g++-14"))
    assert "--repo_env=CC=gcc-14" in calls[0]
    assert "--repo_env=CXX=g++-14" in calls[0]
    # Flags must precede the targets.
    assert calls[0].index("--repo_env=CC=gcc-14") < calls[0].index("//a")


def test_parity_job_pins_the_bazel_leg():
    """Otherwise the comparison varies build system AND producer at once."""
    from pathlib import Path as _Path

    workflow = (
        _Path(__file__).resolve().parent.parent
        / ".github" / "workflows" / "integration-shadow.yml"
    ).read_text(encoding="utf-8")
    assert "--bazel-toolchain gcc-14,g++-14" in workflow
