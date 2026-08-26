"""capabilities.yaml's toolchain axis must match what the job actually runs.

Codex found one instance of this on PR #30: the `loader-features` job was
labelled `gcc14-cxx17` while installing no GCC 14 and letting the runner
choose, so the coverage was recorded under a toolchain it never exercised.
It turned out to affect four of the new depth-scenario entries. A label
nothing checks is the same failure mode capabilities.yaml exists to prevent,
so this checks it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: Toolchain labels that assert a specific pinned producer. Anything else
#: ("pinned-default", "multi-producer", "n/a") makes no such claim and is
#: not checked here.
PINNED_LABELS = {
    "gcc14-cxx17": ("gcc-14", "g++-14"),
    "clang18-cxx17": ("clang-18", "clang++-18"),
}


def _matrix() -> dict:
    return yaml.safe_load((REPO_ROOT / "capabilities.yaml").read_text(encoding="utf-8"))


def _job_text(workflow: str, job: str) -> str:
    """Everything that decides which compiler this job actually runs.

    For a normal job that is its steps. For a reusable-workflow call there
    are no steps: what pins its toolchain is the trusted bindings file it
    forwards, so that file's contents are folded in. Resolving it beats
    relabelling a job that genuinely is pinned, just not by an apt install
    of its own.
    """
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    definition = document["jobs"][job]
    steps = definition.get("steps")
    if steps is not None:
        return yaml.safe_dump(steps)
    text = yaml.safe_dump(definition)
    bindings = (definition.get("with") or {}).get("toolchain-bindings-path")
    if bindings:
        path = REPO_ROOT / bindings
        if not path.is_file():
            raise AssertionError(
                f"{workflow}:{job} forwards toolchain-bindings-path={bindings!r}, "
                "which does not exist"
            )
        text += path.read_text(encoding="utf-8")
    return text


def _pinned_entries() -> list:
    entries = []
    for entry in _matrix()["capabilities"]:
        label = entry.get("dimensions", {}).get("toolchain")
        if label in PINNED_LABELS and entry.get("workflow") and entry.get("job"):
            entries.append(entry)
    return entries


def test_there_are_pinned_toolchain_entries_to_check():
    """Guard against this file silently passing on an empty set."""
    assert _pinned_entries()


@pytest.mark.parametrize(
    "entry", _pinned_entries(), ids=lambda e: e["id"]
)
def test_pinned_toolchain_label_is_actually_exercised(entry: dict):
    label = entry["dimensions"]["toolchain"]
    executables = PINNED_LABELS[label]
    text = _job_text(entry["workflow"], entry["job"])
    assert any(exe in text for exe in executables), (
        f"{entry['id']} is labelled toolchain={label!r} but "
        f"{entry['workflow']}:{entry['job']} never names any of "
        f"{executables} -- it would run on whatever compiler the runner "
        f"image ships, recording coverage under a toolchain it never used"
    )


@pytest.mark.parametrize(
    "job",
    ["project-cross-dso", "l2-green-l4-red", "loader-features", "runtime-floor"],
)
def test_depth_scenario_jobs_pass_the_compiler_explicitly(job: str):
    """Installing gcc-14 is not enough; the runner must be told to use it.

    Every one of these runners defaults to `shutil.which("gcc")`/`"g++"`,
    which resolves to the image's default (gcc-13 on ubuntu-24.04) even
    when gcc-14 is installed alongside it.
    """
    text = _job_text("depth-scenarios.yml", job)
    assert re.search(r"--(cc|cxx)\s+(gcc|g\+\+)-14", text), (
        f"depth-scenarios.yml:{job} does not pass an explicit --cc/--cxx"
    )


def test_producer_compiler_job_installs_both_producers():
    """Its whole point is two producers; one missing makes it vacuous."""
    text = _job_text("depth-scenarios.yml", "producer-compiler")
    assert "g++-14" in text
    assert "clang-18" in text
