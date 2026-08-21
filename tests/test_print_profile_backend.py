"""Unit tests for ci/print_profile_backend.py -- the lookup three
workflows (integration-shadow.yml, profile-baseline.yml, release.yml) use
to decide whether to install the real abicheck scanner + CastXML for a
matrix leg, instead of hardcoding today's cmake/make profile ids in a
workflow `if:` condition (Codex review, PR #25: a hardcoded predicate
silently stops installing the scanner the moment a third cmake/make
profile is added).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ci" / "print_profile_backend.py"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_known_cmake_profile():
    proc = _run("--profile-id", "linux-x86_64-gcc14-cxx17-cmake-ninja")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "cmake"


def test_known_bazel_profile():
    proc = _run("--profile-id", "linux-x86_64-gcc14-cxx17-bazel")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "bazel"


def test_known_make_profile():
    proc = _run("--profile-id", "linux-x86_64-gcc14-cxx17-make-bear")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "make"


def test_unknown_profile_fails_closed():
    proc = _run("--profile-id", "nonexistent-profile")
    assert proc.returncode == 1
    assert "nonexistent-profile" in proc.stderr


def test_custom_profiles_file(tmp_path):
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n"
        "  - id: my-custom-profile\n"
        "    backend: make\n"
    )
    proc = _run("--profile-id", "my-custom-profile", "--profiles-file", str(profiles_file))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "make"
