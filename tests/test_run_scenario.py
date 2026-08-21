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
