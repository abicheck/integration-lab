"""Unit tests for ci/check_profile.py -- the real nm/readelf
dynamic-symbol-table diff mechanism PR2 uses in place of the unreachable
(no network in this sandbox) external `abicheck` scanner.

Builds real, tiny shared objects with `gcc` (skipped, not failed, when gcc
isn't on PATH -- matching tests/test_backends.py's own precedent) rather
than mocking nm/readelf output -- the whole point of this script is that it
shells out to the real toolchain.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from check_profile import CheckProfileError, build_baseline, compare_profile, dynamic_symbols

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")


def _compile_shared_lib(tmp_path, source: str, name: str = "libfake.so", extra_args=()):
    src = tmp_path / "lib.c"
    src.write_text(source)
    out = tmp_path / name
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-o", str(out), str(src), *extra_args],
        check=True, capture_output=True, text=True,
    )
    return out


def _stage_profile(tmp_path, lib_path, header_text="int foo(void);\nint bar(void);\n", profile_id="p1", staged_name="libmath.so"):
    # staged_name is deliberately a fixed, canonical target filename
    # (mirroring how a real profile's target -- e.g. libmath.so -- keeps
    # the same on-disk name across rebuilds), independent of whatever
    # scratch filename _compile_shared_lib happened to use for this
    # particular test compile.
    staged = tmp_path / "staged"
    (staged / "artifacts" / "lib").mkdir(parents=True)
    shutil.copy2(lib_path, staged / "artifacts" / "lib" / staged_name)
    headers_dir = staged / "headers" / "include"
    headers_dir.mkdir(parents=True)
    (headers_dir / "api.h").write_text(header_text)
    build_output = {
        "schema_version": 1,
        "profile": {"id": profile_id, "backend": "make"},
        "targets": {
            "math": {
                "kind": "shared_library",
                "built": True,
                "path": f"artifacts/lib/{staged_name}",
                "sha256": None,
                "size_bytes": None,
            }
        },
    }
    (staged / "build-output.json").write_text(json.dumps(build_output))
    return staged


def test_dynamic_symbols_finds_exported_function(tmp_path):
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    symbols = dynamic_symbols(lib)
    names = {s["name"] for s in symbols}
    assert "foo" in names


def test_dynamic_symbols_excludes_linker_artifacts(tmp_path):
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    symbols = dynamic_symbols(lib)
    names = {s["name"] for s in symbols}
    assert "_edata" not in names
    assert "_end" not in names
    assert "__bss_start" not in names


def test_compare_profile_no_change_for_identical_build(tmp_path):
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)
    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NO_CHANGE"
    assert result["symbols"]["removed"] == []
    assert result["symbols"]["added"] == []


def test_compare_profile_breaking_on_removed_symbol(tmp_path):
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before)
    baseline = build_baseline("p1", "math", baseline_staged)

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after)
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "BREAKING"
    assert "bar" in result["symbols"]["removed"]


def test_compare_profile_compatible_on_added_symbol(tmp_path):
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before)
    baseline = build_baseline("p1", "math", baseline_staged)

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n", name="lib_after.so")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after)
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "COMPATIBLE"
    assert "bar" in result["symbols"]["added"]


def test_compare_profile_compatible_on_added_symbol_with_matching_header_update(tmp_path):
    # The realistic case: adding a public function almost always means
    # declaring it in the header too. headers_changed being non-empty here
    # must not override the COMPATIBLE verdict the symbol table itself
    # already proves -- the previous check order (header-only fallback
    # checked before "added") made this overwhelmingly common, safe case
    # incorrectly report NOT_COMPARABLE (Codex review, PR #20). The
    # existing test_compare_profile_compatible_on_added_symbol above never
    # actually exercised this since it doesn't change header_text between
    # before/after.
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, header_text="int foo(void);\n")
    baseline = build_baseline("p1", "math", baseline_staged)

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n", name="lib_after.so")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, header_text="int foo(void);\nint bar(void);\n")
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "COMPATIBLE"
    assert "bar" in result["symbols"]["added"]
    assert result["public_headers_changed"]


def test_compare_profile_not_comparable_on_header_only_change(tmp_path):
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    baseline_staged = _stage_profile(tmp_path / "before", lib, header_text="int foo(void); // v1\n")
    baseline = build_baseline("p1", "math", baseline_staged)

    # Same symbol table, only the header's content changed -- this
    # mechanism has no way to tell a comment/whitespace edit apart from a
    # real API surface change at the symbol-table level, so it must not
    # claim COMPATIBLE (a proven-safe verdict this signal can't back up).
    candidate_staged = _stage_profile(tmp_path / "after", lib, header_text="int foo(void); // v2, changed\n")
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "NOT_COMPARABLE"
    assert result["public_headers_changed"]


def test_compare_profile_not_breaking_on_code_symbol_size_change(tmp_path):
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before)
    baseline = build_baseline("p1", "math", baseline_staged)

    # Same exported symbol, same header -- but a larger function body
    # (more code, same signature) legitimately changes the compiled size
    # of a code symbol on an ordinary recompile. That's not ABI-relevant
    # on its own and must not report BREAKING.
    lib_after = _compile_shared_lib(
        tmp_path,
        "int foo(void) { int x = 1; for (int i = 0; i < 100; i++) { x += i * 2 - 1; } return x; }\n",
        name="lib_after.so",
    )
    candidate_staged = _stage_profile(tmp_path / "after", lib_after)
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "NO_CHANGE"
    assert result["symbols"]["changed"] == []


def test_compare_profile_breaking_on_soname_change(tmp_path):
    lib_before = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so", extra_args=["-Wl,-soname,libmath.so.1"]
    )
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    baseline = build_baseline("p1", "math", baseline_staged)
    assert baseline["soname"] == "libmath.so.1"

    lib_after = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so", extra_args=["-Wl,-soname,libmath.so.2"]
    )
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="libmath.so")
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "BREAKING"
    assert "SONAME changed" in result["detail"]


def test_compare_profile_breaking_on_filename_change_without_soname(tmp_path):
    # Bazel's cc_shared_library outputs carry no SONAME, so the loader
    # locates them by their on-disk filename instead. A rename here is
    # just as breaking to existing consumers as a removed symbol would be.
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    baseline = build_baseline("p1", "math", baseline_staged)
    assert baseline["soname"] is None  # gcc -shared with no -Wl,-soname

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="librenamed.so")
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "BREAKING"
    assert "loader filename changed" in result["detail"]


def test_compare_profile_not_comparable_on_wrong_staged_profile(tmp_path):
    # staged_dir's own build-output.json says which profile actually
    # produced it -- a caller passing --profile-id p1 alongside a
    # staged_dir that build-output.json itself says belongs to a
    # different profile must not be trusted just because the symbol
    # tables happen to match.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    baseline_staged = _stage_profile(tmp_path, lib, profile_id="p1")
    baseline = build_baseline("p1", "math", baseline_staged)

    wrong_profile_staged = _stage_profile(tmp_path / "wrong", lib, profile_id="p2")
    result = compare_profile("p1", "math", wrong_profile_staged, baseline)

    assert result["verdict"] == "NOT_COMPARABLE"
    assert "not the requested profile_id" in result["detail"]


def test_build_baseline_rejects_wrong_staged_profile(tmp_path):
    # dump's --profile-id and --staged-dir are two independent CLI
    # arguments -- nothing stops a caller from stamping a p2-built staged
    # dir's baseline as p1. compare_profile()'s identity check can't catch
    # this after the fact since a baseline poisoned this way carries
    # self-consistent (but wrong) profile_id/target metadata.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    wrong_profile_staged = _stage_profile(tmp_path, lib, profile_id="p2")

    with pytest.raises(CheckProfileError, match="not the requested profile_id"):
        build_baseline("p1", "math", wrong_profile_staged)


def test_compare_profile_not_comparable_on_missing_target(tmp_path):
    # A stub build-output.json with no "profile" block at all previously
    # made this test pass through the *staged-identity* guard (staged_
    # profile_id is None != "p1") instead of the missing-target branch it
    # was meant to exercise, so the missing-target code path itself went
    # untested (CodeRabbit review, PR #20). Include a matching profile
    # block so the identity guard passes and this reaches _target_library_
    # path()'s own "no target" check.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)

    empty_staged = tmp_path / "empty-staged"
    empty_staged.mkdir()
    (empty_staged / "build-output.json").write_text(
        json.dumps({"profile": {"id": "p1", "backend": "make"}, "targets": {}})
    )
    result = compare_profile("p1", "math", empty_staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "has no target" in result["detail"]
