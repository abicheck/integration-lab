"""Unit tests for ci/compare_build_outputs.py (PR3 item 1).

Builds real, tiny shared objects with `gcc` (skipped, not failed, when gcc
isn't on PATH -- matching tests/test_check_profile.py's own precedent)
rather than mocking nm/readelf output, since the whole point of this
script is that it shells out to the real toolchain.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from compare_build_outputs import compare_all, compare_pair, compare_target

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")


def _compile_shared_lib(tmp_path, source: str, name: str, soname: str = None, extra_args=()):
    src = tmp_path / f"{name}.c"
    src.write_text(source)
    out = tmp_path / name
    args = ["gcc", "-shared", "-fPIC", "-o", str(out), str(src), *extra_args]
    if soname:
        args += ["-Wl,-soname," + soname]
    subprocess.run(args, check=True, capture_output=True, text=True)
    return out


def _stage(tmp_path, profile_id, backend, lib_path, header_text, target="math", extra_targets=None):
    staged = tmp_path / f"staged-{profile_id}"
    (staged / "artifacts" / "lib").mkdir(parents=True)
    staged_name = f"lib{target}.so"
    shutil.copy2(lib_path, staged / "artifacts" / "lib" / staged_name)
    headers_dir = staged / "headers" / "include"
    headers_dir.mkdir(parents=True)
    (headers_dir / "api.h").write_text(header_text)
    targets = {
        target: {
            "kind": "shared_library",
            "built": True,
            "path": f"artifacts/lib/{staged_name}",
            "sha256": None,
            "size_bytes": None,
        }
    }
    if extra_targets:
        targets.update(extra_targets)
    build_output = {
        "schema_version": 1,
        "git": {"sha": "deadbeef"},
        "profile": {"id": profile_id, "backend": backend, "root": ".", "generator": None},
        "compiler": {"family": "gcc", "standard": "c++17"},
        "targets": targets,
    }
    (staged / "build-output.json").write_text(json.dumps(build_output))
    return staged


def _profiles_yaml(tmp_path, ids, checked_targets=("math",)):
    path = tmp_path / "profiles.yaml"
    entries = "\n".join(
        f"""  - id: {pid}
    backend: {pid.split('-')[-1]}
    contract: false
    root: "."
    generator: null
    compiler: {{family: gcc, cc: gcc-14, cxx: g++-14, standard: c++17}}
    targets: {{math: math}}
    header_roots: [include]
    build_command: "true"
    coverage:
      checked_targets: {list(checked_targets)!r}
"""
        for pid in ids
    )
    path.write_text(f"schema_version: 1\nprofiles:\n{entries}")
    return path


def test_compare_target_equivalent_for_identical_source(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")

    build_output_a = json.loads((staged_a / "build-output.json").read_text())
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    result = compare_target("math", staged_a, build_output_a, staged_b, build_output_b, run_abicheck=False)
    assert result["verdict"] == "EQUIVALENT"
    assert result["differences"] == []


def test_compare_target_flags_removed_symbol_as_public_contract_mismatch(tmp_path):
    lib_a = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n", "liba.so", soname="libmath.so"
    )
    lib_b = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void); int bar(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")

    build_output_a = json.loads((staged_a / "build-output.json").read_text())
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    result = compare_target("math", staged_a, build_output_a, staged_b, build_output_b, run_abicheck=False)
    assert result["verdict"] == "DIVERGENT"
    categories = {d["category"] for d in result["differences"]}
    assert "public_contract_mismatch" in categories


def test_compare_target_flags_soname_mismatch(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so.1")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")

    build_output_a = json.loads((staged_a / "build-output.json").read_text())
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    result = compare_target("math", staged_a, build_output_a, staged_b, build_output_b, run_abicheck=False)
    assert result["verdict"] == "DIVERGENT"
    soname_diffs = [d for d in result["differences"] if d["aspect"] == "soname"]
    assert len(soname_diffs) == 1
    assert soname_diffs[0]["category"] == "public_contract_mismatch"


def test_compare_target_flags_header_content_mismatch_as_unclassified(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void); /* stray comment */\n")

    build_output_a = json.loads((staged_a / "build-output.json").read_text())
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    result = compare_target("math", staged_a, build_output_a, staged_b, build_output_b, run_abicheck=False)
    assert result["verdict"] == "DIVERGENT"
    content_diffs = [d for d in result["differences"] if d["aspect"] == "public_header_content"]
    assert len(content_diffs) == 1
    assert content_diffs[0]["category"] == "unclassified_difference"


def test_compare_target_not_comparable_when_target_missing_on_one_side(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")

    build_output_a = json.loads((staged_a / "build-output.json").read_text())
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    result = compare_target("strings", staged_a, build_output_a, staged_b, build_output_b, run_abicheck=False)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "strings" in result["reason"]


def test_compare_pair_excludes_bazel_only_target_from_target_set_mismatch(tmp_path):
    """A shared-library target one side builds beyond the canonical
    checked_targets set (e.g. bazel's math_shared) must be recorded as
    expected_build_system_metadata_difference context, never surfaced as
    an unclassified/public-contract finding or dragged into the verdict.
    """
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    extra = {
        "math_shared": {
            "kind": "shared_library",
            "built": True,
            "path": "artifacts/lib/libmath_shared.so",
            "sha256": None,
            "size_bytes": None,
        }
    }
    staged_a = _stage(tmp_path, "p-a", "bazel", lib_a, "int foo(void);\n", extra_targets=extra)
    staged_b = _stage(tmp_path, "p-b", "cmake", lib_b, "int foo(void);\n")

    result = compare_pair("p-a", staged_a, "p-b", staged_b, ["math"], run_abicheck=False)
    assert result["verdict"] == "EQUIVALENT"
    categories = {d["category"] for d in result["context_differences"]}
    assert "unclassified_difference" not in categories
    assert "public_contract_mismatch" not in categories
    metadata_diffs = [d for d in result["context_differences"] if d["aspect"] == "build_system_specific_targets"]
    assert len(metadata_diffs) == 1
    assert metadata_diffs[0]["value_a"] == ["math_shared"]


def test_compare_all_requires_at_least_two_build_dirs(tmp_path):
    from compare_build_outputs import CompareBuildOutputsError

    with pytest.raises(CompareBuildOutputsError):
        compare_all({"only-one": tmp_path}, profiles_path=_profiles_yaml(tmp_path, ["only-one"]))


def test_compare_all_overall_verdict_equivalent_for_matching_builds(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")
    profiles_path = _profiles_yaml(tmp_path, ["p-a", "p-b"])

    doc = compare_all({"p-a": staged_a, "p-b": staged_b}, profiles_path=profiles_path, run_abicheck=False)
    assert doc["overall_verdict"] == "EQUIVALENT"
    assert doc["commit_diagnostic"] is None
    assert len(doc["pairs"]) == 1


def test_compare_all_flags_mismatched_commits(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")
    build_output_b = json.loads((staged_b / "build-output.json").read_text())
    build_output_b["git"]["sha"] = "cafef00d"
    (staged_b / "build-output.json").write_text(json.dumps(build_output_b))
    profiles_path = _profiles_yaml(tmp_path, ["p-a", "p-b"])

    doc = compare_all({"p-a": staged_a, "p-b": staged_b}, profiles_path=profiles_path, run_abicheck=False)
    assert doc["commit_diagnostic"] is not None
    assert "different git commits" in doc["commit_diagnostic"]


def test_compare_all_not_comparable_when_no_shared_checked_targets(tmp_path):
    src = "int foo(void) { return 1; }\n"
    lib_a = _compile_shared_lib(tmp_path, src, "liba.so", soname="libmath.so")
    lib_b = _compile_shared_lib(tmp_path, src, "libb.so", soname="libmath.so")
    staged_a = _stage(tmp_path, "p-a", "cmake", lib_a, "int foo(void);\n")
    staged_b = _stage(tmp_path, "p-b", "make", lib_b, "int foo(void);\n")
    # p-b declares a disjoint checked_targets set from p-a, so the
    # intersection is empty -- must fail closed as NOT_COMPARABLE/
    # INCOMPLETE, never silently report EQUIVALENT for nothing compared.
    path = tmp_path / "profiles.yaml"
    path.write_text(
        """schema_version: 1
profiles:
  - id: p-a
    backend: cmake
    contract: false
    root: "."
    generator: null
    compiler: {family: gcc, cc: gcc-14, cxx: g++-14, standard: c++17}
    targets: {math: math}
    header_roots: [include]
    build_command: "true"
    coverage:
      checked_targets: [math]
  - id: p-b
    backend: make
    contract: false
    root: "."
    generator: null
    compiler: {family: gcc, cc: gcc-14, cxx: g++-14, standard: c++17}
    targets: {math: math}
    header_roots: [include]
    build_command: "true"
    coverage:
      checked_targets: [strings]
"""
    )
    doc = compare_all({"p-a": staged_a, "p-b": staged_b}, profiles_path=path, run_abicheck=False)
    assert doc["overall_verdict"] == "INCOMPLETE"
    assert doc["pairs"][0]["verdict"] == "NOT_COMPARABLE"
