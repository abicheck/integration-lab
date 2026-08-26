"""The Make fixture recipe must rebuild when a fixture HEADER changes.

Codex review. `$(OUT): $(SRC)` listed lib.cc as the only prerequisite, so a
changed lib.h left libimpl.so looking current and make skipped the rebuild.
That is reachable with default arguments: run_scenario.py's scratch directory
is `<results-dir>/make-scratch` and --results-dir defaults to the persistent
`scenario-results`, so a second run after editing a fixture header compared a
STALE binary against the new header and reported findings for the previous
pair.

Worse than plain staleness: CMake and Bazel track header dependencies on
their own, so only the Make leg would go stale and check_scenario_parity.py
would report it as a cross-build-system DISAGREEMENT -- a false positive in
the gate whose job is telling real disagreements from noise.

These drive the real makefile with a real compiler rather than asserting on
its text, because the property under test is what make DOES.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "buildsystems" / "make" / "fixtures" / "Makefile"

pytestmark = pytest.mark.skipif(
    not shutil.which("make") or not shutil.which("g++"),
    reason="needs make and g++",
)

HEADER_V1 = "struct S { int a; };\nint f(S);\n"
HEADER_V2 = "struct S { int a; long b; };\nint f(S);\n"
SOURCE = '#include "lib.h"\nint f(S s) { return s.a; }\n'


def _build(fixture: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-f", str(MAKEFILE), f"FIXTURE_DIR={fixture}",
         f"BUILD_DIR={out}", "CXX=g++"],
        cwd=REPO_ROOT, text=True, capture_output=True,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "lib.h").write_text(HEADER_V1)
    (fixture / "lib.cc").write_text(SOURCE)
    return fixture


def test_a_changed_header_rebuilds_the_library(fixture_dir, tmp_path):
    """The bug: this is what silently did not happen."""
    out = tmp_path / "out"
    assert _build(fixture_dir, out).returncode == 0
    before = _digest(out / "libimpl.so")

    (fixture_dir / "lib.h").write_text(HEADER_V2)
    result = _build(fixture_dir, out)
    assert result.returncode == 0, result.stderr
    assert _digest(out / "libimpl.so") != before, (
        "libimpl.so was not rebuilt after its header changed -- the comparison "
        "would run against a stale binary"
    )


def test_a_changed_source_still_rebuilds(fixture_dir, tmp_path):
    """The prerequisite that already worked must keep working."""
    out = tmp_path / "out"
    assert _build(fixture_dir, out).returncode == 0
    before = _digest(out / "libimpl.so")

    (fixture_dir / "lib.cc").write_text(SOURCE.replace("s.a", "s.a + 1"))
    assert _build(fixture_dir, out).returncode == 0
    assert _digest(out / "libimpl.so") != before


def test_an_unchanged_fixture_is_not_rebuilt(fixture_dir, tmp_path):
    """Dependency tracking must not degrade into rebuilding every time --
    that would hide a staleness bug rather than fix it."""
    out = tmp_path / "out"
    assert _build(fixture_dir, out).returncode == 0
    before = (out / "libimpl.so").stat().st_mtime_ns
    second = _build(fixture_dir, out)
    assert second.returncode == 0
    # Asserted on the ARTIFACT rather than on make's message. The message
    # check was a proxy, and it stopped holding when the fixture-identity
    # stamp was added below: the stamp target is .PHONY, so make always has
    # something to consider and prints nothing. The property this test is
    # actually about -- the library was not rebuilt -- is unchanged, and
    # checking it directly is stronger than matching output text.
    assert (out / "libimpl.so").stat().st_mtime_ns == before, (
        second.stdout + second.stderr
    )


def test_a_deleted_header_does_not_wedge_make(tmp_path):
    """-MP emits phony targets for each header, so removing one is a rebuild
    rather than "no rule to make target"."""
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "extra.h").write_text(HEADER_V1)
    (fixture / "lib.h").write_text('#include "extra.h"\n')
    (fixture / "lib.cc").write_text('#include "lib.h"\nint f(S s) { return s.a; }\n')
    out = tmp_path / "out"
    assert _build(fixture, out).returncode == 0

    (fixture / "extra.h").unlink()
    (fixture / "lib.h").write_text(HEADER_V1)
    result = _build(fixture, out)
    assert result.returncode == 0, result.stderr


def test_clean_removes_the_dependency_file_too(fixture_dir, tmp_path):
    out = tmp_path / "out"
    assert _build(fixture_dir, out).returncode == 0
    assert (out / "libimpl.d").is_file(), "the .d file is what carries the header deps"
    subprocess.run(
        ["make", "-f", str(MAKEFILE), f"FIXTURE_DIR={fixture_dir}",
         f"BUILD_DIR={out}", "clean"],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )
    assert not (out / "libimpl.so").exists()
    assert not (out / "libimpl.d").exists(), "a stale .d outliving its .so is its own hazard"


# --- re-pointing a build dir at a different fixture (Codex review) --------
#
# OUT and DEP are keyed on BUILD_DIR alone while SRC is keyed on
# FIXTURE_DIR. run_scenario.py names its build dir after the SCENARIO
# (`<scratch>/<name>-old`) and takes the fixture from
# scenarios/build-matrix.yaml, so re-pointing a scenario at a different
# fixture reuses the build dir with a different source. Fixture files are
# checked-out sources whose mtimes predate the previous build, so make saw
# "Nothing to be done" and kept the PREVIOUS FIXTURE's library.


def test_repointing_the_build_dir_at_another_fixture_rebuilds(tmp_path):
    """Reproduced against the pre-fix recipe: without the fixture stamp the
    second build is skipped and the first fixture's library survives."""
    first = tmp_path / "one"
    first.mkdir()
    (first / "lib.h").write_text(HEADER_V1)
    (first / "lib.cc").write_text(SOURCE)

    second = tmp_path / "two"
    second.mkdir()
    (second / "lib.h").write_text(HEADER_V1)
    # A distinguishable body, so a stale binary is detectable by content.
    (second / "lib.cc").write_text(SOURCE.replace("return 1;", "return 2;"))

    out = tmp_path / "build"
    out.mkdir()
    assert _build(first, out).returncode == 0
    built_first = _digest(out / "libimpl.so")

    result = _build(second, out)
    assert result.returncode == 0, result.stderr
    assert _digest(out / "libimpl.so") != built_first, (
        "re-pointing BUILD_DIR at a different fixture left the previous "
        "fixture's library in place"
    )


def test_the_fixture_stamp_records_which_fixture_the_build_dir_holds(tmp_path):
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "lib.h").write_text(HEADER_V1)
    (fixture / "lib.cc").write_text(SOURCE)
    out = tmp_path / "build"
    out.mkdir()
    assert _build(fixture, out).returncode == 0
    assert (out / "libimpl.fixture").read_text().strip() == str(fixture)


def test_rebuilding_the_same_fixture_is_still_a_no_op(tmp_path):
    """The stamp must not defeat incremental builds: it is rewritten only
    when the fixture actually changes."""
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "lib.h").write_text(HEADER_V1)
    (fixture / "lib.cc").write_text(SOURCE)
    out = tmp_path / "build"
    out.mkdir()
    assert _build(fixture, out).returncode == 0
    before = (out / "libimpl.so").stat().st_mtime_ns
    result = _build(fixture, out)
    assert result.returncode == 0, result.stderr
    assert (out / "libimpl.so").stat().st_mtime_ns == before, result.stdout


def test_clean_removes_the_fixture_stamp(tmp_path):
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "lib.h").write_text(HEADER_V1)
    (fixture / "lib.cc").write_text(SOURCE)
    out = tmp_path / "build"
    out.mkdir()
    assert _build(fixture, out).returncode == 0
    subprocess.run(
        ["make", "-f", str(MAKEFILE), f"FIXTURE_DIR={fixture}",
         f"BUILD_DIR={out}", "clean"],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )
    assert not (out / "libimpl.fixture").exists()
