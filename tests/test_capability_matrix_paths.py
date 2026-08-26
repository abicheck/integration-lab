"""Every data input the capability-matrix checks read must trigger them.

Codex has now found this same gap three times (PR #16 was the first): a
check lands, it reads some file, and `.github/workflows/capability-matrix.yml`'s
`pull_request.paths` filter does not list it -- so a PR touching only that
file skips the very check meant to police it. The pin-consistency suite
(ci/abicheck-version.yaml) and the generated README gap block
(scenarios/manifest.yaml) were both reachable that way.

Adding two lines would have fixed those two and left the pattern intact. So
this derives the requirement instead: whatever DATA file the workflow's
scripts and tests actually open must be matched by the filter. Scripts
themselves are already covered by the scripts/*.py globs; this is about the
inputs they read.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "capability-matrix.yml"

#: `REPO_ROOT / "a" / "b.yaml"` and `REPO_ROOT / "a.yaml"`, the two forms the
#: checked scripts use to name a repo-relative input.
_ROOTED = re.compile(
    r'REPO_ROOT\s*(?:/\s*"([A-Za-z0-9_.-]+)"\s*){1,3}'
)
_SEGMENT = re.compile(r'"([A-Za-z0-9_.-]+)"')
DATA_SUFFIXES = {".yaml", ".yml", ".json"}


def _declared_paths() -> list[str]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")) or {}
    # `on:` parses as the boolean True in YAML 1.1.
    triggers = document.get(True) or document.get("on") or {}
    return list((triggers.get("pull_request") or {}).get("paths") or [])


def _matches(pattern: str, path: str) -> bool:
    """GitHub path-filter semantics: `*` does not cross `/`, `**` does."""
    parts = []
    for chunk in re.split(r"(\*\*|\*)", pattern):
        if chunk == "**":
            parts.append(".*")
        elif chunk == "*":
            parts.append("[^/]*")
        else:
            parts.append(re.escape(chunk))
    return re.fullmatch("".join(parts), path) is not None


def _data_inputs() -> set[str]:
    found: set[str] = set()
    sources = sorted((REPO_ROOT / "scripts").glob("*.py")) + sorted(
        (REPO_ROOT / "tests").glob("*.py")
    )
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for match in _ROOTED.finditer(text):
            segments = _SEGMENT.findall(match.group(0))
            candidate = "/".join(segments)
            if Path(candidate).suffix not in DATA_SUFFIXES:
                continue
            if (REPO_ROOT / candidate).is_file():
                found.add(candidate)
    return found


def test_the_derivation_finds_the_known_inputs():
    """A derivation that finds nothing would pass vacuously forever."""
    inputs = _data_inputs()
    assert {
        "capabilities.yaml",
        "ci/abicheck-version.yaml",
        "scenarios/manifest.yaml",
    } <= inputs, sorted(inputs)


def test_every_data_input_triggers_the_workflow():
    patterns = _declared_paths()
    assert patterns, "the workflow declares a pull_request paths filter"
    missing = sorted(
        path
        for path in _data_inputs()
        if not any(_matches(pattern, path) for pattern in patterns)
    )
    assert not missing, (
        "these files are read by the capability-matrix checks but do not "
        "appear in .github/workflows/capability-matrix.yml's "
        f"pull_request.paths, so a PR touching only them skips the check "
        f"meant to police them: {missing}"
    )


def test_the_matcher_respects_path_boundaries():
    """`*` must not cross `/`, or the filter would look broader than it is."""
    assert _matches("scripts/check_*.py", "scripts/check_demo_oracle.py")
    assert not _matches("scripts/check_*.py", "scripts/sub/check_x.py")
    assert _matches("tests/**", "tests/a/b/c.py")
    assert _matches("capabilities.yaml", "capabilities.yaml")
    assert not _matches("capabilities.yaml", "other/capabilities.yaml")
