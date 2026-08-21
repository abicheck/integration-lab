"""Unit tests for ci/check_profile.py -- the real nm/readelf
dynamic-symbol-table diff mechanism PR2 uses in place of the unreachable
(no network in this sandbox) external `abicheck` scanner.

Builds real, tiny shared objects with `gcc` (skipped, not failed, when gcc
isn't on PATH -- matching tests/test_backends.py's own precedent) rather
than mocking nm/readelf output -- the whole point of this script is that it
shells out to the real toolchain.

These fixtures stage a synthetic build_output with backend "bazel" -- not
because these tests are Bazel-specific, but because a later PR
(ci/real_scan.py) wires cmake/make backends to a REAL `abicheck
dump`/`compare` invocation requiring an actual compile_commands.json this
synthetic single-.c-file fixture doesn't have. "bazel" is the one backend
that still exercises exactly the nm/readelf mechanism these tests target,
unconditionally, with no real-scan branch to satisfy. See
tests/test_real_scan.py for the real-scan module's own unit tests, and
test_check_profile.py's own test_compare_profile_real_scan_* tests below
for the cmake/make override behavior.
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
        "profile": {"id": profile_id, "backend": "bazel"},
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


def test_compare_profile_breaking_when_soname_named_file_missing(tmp_path):
    # An unchanged SONAME string doesn't by itself prove the library is
    # loadable under that name -- the actual file (or symlink) named
    # after the SONAME has to exist in the staged output too (this is
    # exactly what ci/backends/cmake.py's own stage() stages a second
    # copy for). Same SONAME on both sides, but the SONAME-named copy is
    # simply never staged: a real loadability break this mechanism
    # previously had no way to detect, since it only compared the SONAME
    # *string* embedded in the file's own ELF metadata.
    lib_before = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so", extra_args=["-Wl,-soname,libmath.so.1"]
    )
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    baseline = build_baseline("p1", "math", baseline_staged)
    assert baseline["soname"] == "libmath.so.1"

    lib_after = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so", extra_args=["-Wl,-soname,libmath.so.1"]
    )
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="libmath.so")
    # No libmath.so.1 written alongside libmath.so in candidate_staged --
    # simulating a backend that failed to stage the SONAME-named copy.
    result = compare_profile("p1", "math", candidate_staged, baseline)

    assert result["verdict"] == "BREAKING"
    assert "no file named that exists" in result["detail"]


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


def test_compare_profile_not_comparable_on_baseline_missing_dynamic_symbols(tmp_path):
    # A baseline with correct kind/profile_id/target but a missing (or
    # truncated-write-corrupted) dynamic_symbols field previously
    # defaulted to [] -- an empty base symbol table makes every candidate
    # export look "newly added" with nothing to diff against, landing in
    # the added -> COMPATIBLE branch despite never actually comparing
    # against the real previous ABI at all.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)
    del baseline["dynamic_symbols"]

    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "dynamic_symbols" in result["detail"]


def test_compare_profile_not_comparable_on_baseline_missing_public_headers(tmp_path):
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)
    del baseline["public_headers"]

    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "public_headers" in result["detail"]


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
    # backend "bazel" -- matching baseline's own backend (from
    # _stage_profile's default), so this reaches the missing-target check
    # rather than the separate backend-identity guard.
    (empty_staged / "build-output.json").write_text(
        json.dumps({"profile": {"id": "p1", "backend": "bazel"}, "targets": {}})
    )
    result = compare_profile("p1", "math", empty_staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "has no target" in result["detail"]


def test_compare_profile_real_scan_backend_fails_closed_on_baseline_without_snapshot(tmp_path):
    # cmake/make backends route through ci/real_scan.py (roadmap.md item
    # 1). A baseline predating that integration (no embedded
    # abicheck_snapshot -- e.g. a pre-existing committed
    # abi/profiles/<id>/math.abicheck.json) must never silently fall back
    # to the nm/readelf-only verdict as if the real scanner had agreed --
    # it fails closed to NOT_COMPARABLE with an explicit reason (design
    # doc section 3.9, "no magic fallbacks"). This does not require a real
    # abicheck/castxml toolchain: the identity/kind checks and the
    # nm/readelf pipeline run unchanged first (same symbols, same
    # headers), and this fixture's baseline lacks abicheck_snapshot before
    # ci/real_scan.py is ever invoked.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)  # backend "bazel" -- no abicheck_snapshot

    # Re-stage as a cmake backend (same library/headers -- only the
    # declared backend changes) so compare_profile()'s real-scan branch
    # fires against this same baseline. The baseline's own "backend" must
    # be changed to match too -- otherwise this hits the separate
    # backend-identity guard (see the dedicated test for that below)
    # instead of the missing-abicheck_snapshot path this test targets.
    baseline["backend"] = "cmake"
    assert "abicheck_snapshot" not in baseline

    build_output_path = staged / "build-output.json"
    build_output = json.loads(build_output_path.read_text())
    build_output["profile"]["backend"] = "cmake"
    build_output_path.write_text(json.dumps(build_output))

    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "no embedded abicheck_snapshot" in result["detail"]


def test_compare_profile_backend_mismatch_fails_closed(tmp_path):
    # A profile whose backend was migrated (e.g. cmake -> bazel) while
    # keeping the same profile_id/target passes every identity check that
    # only compares profile_id/target -- but backend is exactly what
    # decides which comparison mechanism runs (real abicheck for
    # cmake/make, nm/readelf for bazel). Comparing across a backend change
    # must fail closed rather than silently falling through to whichever
    # mechanism the candidate's own (possibly weaker) backend selects
    # (Codex review, PR #25).
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)
    baseline["backend"] = "cmake"  # baseline claims a different backend than the staged build

    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NOT_COMPARABLE"
    assert "backend" in result["detail"]
    assert "cmake" in result["detail"] and "bazel" in result["detail"]


def test_compare_profile_missing_baseline_backend_is_not_a_mismatch(tmp_path):
    # A baseline with no "backend" field at all (predating that field, or
    # hand-edited) has no opinion to contradict -- this must not be
    # confused with an actual mismatch and must not itself block the
    # existing nm/readelf comparison from running normally.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\nint bar(void) { return 2; }\n")
    staged = _stage_profile(tmp_path, lib)
    baseline = build_baseline("p1", "math", staged)
    assert "backend" in baseline
    del baseline["backend"]

    result = compare_profile("p1", "math", staged, baseline)
    assert result["verdict"] == "NO_CHANGE"


def test_compare_profile_real_scan_backend_preserves_loader_break(tmp_path):
    # A loader-level break (the SONAME-named file missing from staged
    # output) is invisible to a real `abicheck compare` -- it only ever
    # inspects the two snapshot files it's handed, not which files are
    # physically staged alongside them on disk. For a cmake/make backend,
    # this must never be silently discarded just because the real-scan
    # path itself can't run (here: baseline predates the real-scanner
    # integration, so this hits _apply_real_scan_verdict's own
    # missing-abicheck_snapshot fail-closed branch) -- the combined
    # verdict must stay BREAKING, not fall back to NOT_COMPARABLE (Codex
    # review, PR #25: "Preserve loader failures when applying the scanner
    # verdict").
    lib_before = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so", extra_args=["-Wl,-soname,libmath.so.1"]
    )
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    baseline = build_baseline("p1", "math", baseline_staged)  # backend "bazel" -- no abicheck_snapshot
    baseline["backend"] = "cmake"  # match the candidate's backend below, avoiding the mismatch guard

    lib_after = _compile_shared_lib(
        tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so", extra_args=["-Wl,-soname,libmath.so.1"]
    )
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="libmath.so")
    # No libmath.so.1 written alongside libmath.so -- the loader-level break.
    build_output_path = candidate_staged / "build-output.json"
    build_output = json.loads(build_output_path.read_text())
    build_output["profile"]["backend"] = "cmake"
    build_output_path.write_text(json.dumps(build_output))

    result = compare_profile("p1", "math", candidate_staged, baseline)
    assert result["verdict"] == "BREAKING"
    assert "no file named that exists" in result["detail"]


def test_build_baseline_degrades_gracefully_when_bear_legitimately_absent(tmp_path):
    # make.py's own verify_environment()/collect_evidence() document a
    # missing `bear` as a non-failing degrade (PR1 item 5;
    # ci/profiles.yaml's `coverage.require_compile_commands: false` for
    # the make-bear profile makes the same tolerance explicit for the
    # coverage gate). Before real_scan.py's real-scanner wiring landed, a
    # runner without bear still got a baseline -- just an nm/readelf-only
    # one. Unconditionally requiring compile_commands.json for the make
    # backend's real-scan embed would instead crash `dump` outright on
    # exactly this documented, non-failing configuration (Codex review,
    # PR #25: "a configuration explicitly supported by the profile can
    # build successfully but can no longer run its ABI signal").
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    build_output_path = staged / "build-output.json"
    build_output = json.loads(build_output_path.read_text())
    build_output["profile"]["backend"] = "make"
    build_output["evidence"] = {
        "backend_evidence": {
            "kind": "make+bear",
            "compile_commands_present": False,
            "note": "bear not installed on this runner -- compile_commands.json not generated",
        }
    }
    build_output_path.write_text(json.dumps(build_output))

    baseline = build_baseline("p1", "math", staged)  # must not raise RealScanError

    assert baseline["abicheck_snapshot"] is None
    assert "bear not installed" in baseline["abicheck_snapshot_skipped_reason"]
    # nm/readelf fields are still a complete baseline on their own.
    assert baseline["dynamic_symbols"]
    assert baseline["public_headers"]


def test_build_baseline_still_fails_closed_on_genuine_compile_db_failure(tmp_path):
    # The bear-absent degrade above must not swallow a real invocation
    # failure on a runner where bear IS installed (integration-shadow.yml
    # explicitly installs it) -- that's still a genuine signal something's
    # broken, matching ci/check_profile_coverage.py's identical
    # tool_absent/genuine-failure distinction for the same evidence.
    lib = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n")
    staged = _stage_profile(tmp_path, lib)
    build_output_path = staged / "build-output.json"
    build_output = json.loads(build_output_path.read_text())
    build_output["profile"]["backend"] = "make"
    build_output["evidence"] = {
        "backend_evidence": {
            "kind": "make+bear",
            "compile_commands_present": False,
            "note": "bear invocation failed: exit 1",
        }
    }
    build_output_path.write_text(json.dumps(build_output))

    with pytest.raises(Exception, match="no compile database"):
        build_baseline("p1", "math", staged)


def test_compare_profile_falls_back_to_nm_readelf_when_bear_legitimately_absent(tmp_path):
    # A baseline dumped with the documented bear-absent degrade
    # (abicheck_snapshot: None + abicheck_snapshot_skipped_reason, see the
    # build_baseline test above) must still be able to produce a passing
    # comparison -- _apply_real_scan_verdict() previously rejected ANY
    # non-dict abicheck_snapshot as NOT_COMPARABLE, indistinguishable from
    # "this baseline predates the real-scanner integration", which made
    # this documented, non-failing configuration unable to ever pass a
    # check or receipt at all (Codex review, PR #25: "Keep Bear-less
    # baselines comparable").
    def _make_build_output(staged_name):
        return {
            "profile": {"id": "p1", "backend": "make"},
            "evidence": {
                "backend_evidence": {
                    "kind": "make+bear",
                    "compile_commands_present": False,
                    "note": "bear not installed on this runner -- compile_commands.json not generated",
                }
            },
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

    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    (baseline_staged / "build-output.json").write_text(json.dumps(_make_build_output("libmath.so")))
    baseline = build_baseline("p1", "math", baseline_staged)
    assert baseline["abicheck_snapshot"] is None  # sanity: this is the degrade path

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="libmath.so")
    (candidate_staged / "build-output.json").write_text(json.dumps(_make_build_output("libmath.so")))

    result = compare_profile("p1", "math", candidate_staged, baseline)
    assert result["verdict"] == "NO_CHANGE"
    assert "bear is not installed" in result["mechanism"]


def test_compare_profile_falls_back_when_candidate_legitimately_lacks_bear(tmp_path):
    # The inverse of the case above: profile-baseline.yml installs bear,
    # so the COMMITTED baseline normally carries a real abicheck_snapshot
    # -- but a local/custom runner comparing against it can still
    # legitimately lack bear itself. The first bear-absent fix only
    # checked the baseline's own abicheck_snapshot_skipped_reason; a
    # baseline WITH a real snapshot compared against a bear-absent
    # candidate fell straight into the generic real-scan-failed except
    # branch and was always forced to NOT_COMPARABLE, so
    # coverage.require_compile_commands: false still could never produce a
    # passing comparison on such a runner (Codex review, PR #25, round 2:
    # "Fall back when the candidate legitimately lacks Bear").
    lib_before = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_before.so")
    baseline_staged = _stage_profile(tmp_path / "before", lib_before, staged_name="libmath.so")
    baseline_build_output = json.loads((baseline_staged / "build-output.json").read_text())
    baseline_build_output["profile"]["backend"] = "make"
    # Bear-absent on THIS build_output too, purely so build_baseline() (this
    # fixture has no real compile_commands.json to give it) degrades
    # gracefully instead of crashing -- overwritten with a fake real
    # snapshot right below to actually simulate the "dumped WITH bear
    # available elsewhere" scenario this test targets.
    baseline_build_output["evidence"] = {
        "backend_evidence": {
            "kind": "make+bear",
            "compile_commands_present": False,
            "note": "bear not installed on this runner -- compile_commands.json not generated",
        }
    }
    (baseline_staged / "build-output.json").write_text(json.dumps(baseline_build_output))
    baseline = build_baseline("p1", "math", baseline_staged)
    # Simulate a baseline dumped WITH bear available (a real, non-null
    # snapshot) -- content doesn't matter, since a bear-absent candidate
    # must never reach the point of actually reading it.
    baseline["abicheck_snapshot"] = {"fake": "snapshot"}
    del baseline["abicheck_snapshot_skipped_reason"]

    lib_after = _compile_shared_lib(tmp_path, "int foo(void) { return 1; }\n", name="lib_after.so")
    candidate_staged = _stage_profile(tmp_path / "after", lib_after, staged_name="libmath.so")
    candidate_build_output = json.loads((candidate_staged / "build-output.json").read_text())
    candidate_build_output["profile"]["backend"] = "make"
    candidate_build_output["evidence"] = {
        "backend_evidence": {
            "kind": "make+bear",
            "compile_commands_present": False,
            "note": "bear not installed on this runner -- compile_commands.json not generated",
        }
    }
    (candidate_staged / "build-output.json").write_text(json.dumps(candidate_build_output))

    result = compare_profile("p1", "math", candidate_staged, baseline)
    assert result["verdict"] == "NO_CHANGE"
    assert "bear is not installed" in result["mechanism"]
