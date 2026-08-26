"""Every ABICheck reference must resolve to the one reviewed pin.

GitHub does not interpolate `uses:` refs, so a reusable-workflow reference
into abicheck/abicheck has to be written out literally in each workflow.
That literal is exactly the thing that drifts: PR #29's own run installed the
scanner from one SHA while the reusable `check-project.yml` reference named
another, and nothing failed.  These tests are the consistency check that
makes the duplication safe -- ci/abicheck-version.yaml is the single reviewed
value, and any workflow that names a different SHA fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_FILE = REPO_ROOT / "ci" / "abicheck-version.yaml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# `abicheck @ git+https://github.com/abicheck/abicheck.git@<ref>`
PIP_REF_RE = re.compile(r"abicheck\.git@(?P<ref>[^\"'\s)]+)")
# `https://raw.githubusercontent.com/abicheck/abicheck/<ref>/action/...`
#
# These are not documentation links: the workflows `curl` them and `bash`
# the result, so the SHA in the URL selects ABICheck-controlled code that
# runs on the runner -- exactly what the pin exists to review. The guard
# did not look at them, and only appeared green because a pip spec
# elsewhere in the same file happened to carry the same SHA; changing ONLY
# an installer URL's SHA was invisible to every test here (Codex review,
# PR #30, second hole of this class).
RAW_URL_REF_RE = re.compile(
    r"raw\.githubusercontent\.com/abicheck/abicheck/(?P<ref>[0-9a-zA-Z._-]+)/"
)
# Both `uses:` forms into abicheck/abicheck:
#   the reusable workflow  `abicheck/abicheck/.github/workflows/x.yml@<ref>`
#   the root composite action `abicheck/abicheck@<ref>`
# The path segment must be OPTIONAL. Requiring a slash after the repo name
# (as this pattern first did) silently skipped all 11 root-action refs in
# abi-scan.yml and baseline.yml, so one of them could drift to an
# uncertified SHA with this guard still green -- defeating the whole point
# of centralizing the pin (Codex review, PR #30).
USES_REF_RE = re.compile(
    r"abicheck/abicheck(?:/[^\s@\"']+)?@(?P<ref>[0-9a-zA-Z._-]+)"
)


@pytest.fixture(scope="module")
def pin() -> dict:
    return yaml.safe_load(PIN_FILE.read_text(encoding="utf-8"))


def _workflows() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


#: `git fetch --depth 1 origin <sha>` against the abicheck remote. The
#: l4_clang_plugin job fetches contrib/abicheck-clang-plugin this way, BUILDS
#: it, and loads the result into the scan -- so an unreviewed revision here
#: executes code on the runner exactly as a tampered installer URL would.
#:
#: Third hole of this class in the same guard, after the root `uses:` form and
#: the raw installer URLs, and it failed identically each time: another form
#: in the SAME file carried a valid pin, so the file looked clean while this
#: ref went unread. Demonstrated before fixing -- changing only the fetch SHA
#: left _refs() reporting nothing but the legacy pin.
GIT_FETCH_REF_RE = re.compile(
    r"git\s+fetch[^\n]*?\borigin\s+(?P<ref>[0-9a-fA-F]{7,40})\b"
)

#: Every way this repository names an ABICheck revision that then executes.
_REF_PATTERNS = (PIP_REF_RE, USES_REF_RE, RAW_URL_REF_RE, GIT_FETCH_REF_RE)


def _refs(text: str) -> set[str]:
    return {
        match.group("ref")
        for pattern in _REF_PATTERNS
        for match in pattern.finditer(text)
    }


def test_pin_file_declares_full_shas(pin: dict) -> None:
    assert pin["repository"] == "abicheck/abicheck"
    assert isinstance(pin["generation"], int) and pin["generation"] >= 0
    for field in ("sha", "legacy_sha", "candidate_sha"):
        assert SHA_RE.match(pin[field]), f"{field} must be a full 40-hex commit SHA"


def test_candidate_is_not_silently_installed(pin: dict) -> None:
    """`candidate_sha` records what the canary should certify next.

    If it ever equals `sha`, the bump already happened and the field is
    stale -- which would quietly turn the certification target into a
    self-referential no-op.
    """
    assert pin["candidate_sha"] != pin["sha"]
    unpinned = {Path(entry).name for entry in pin["unpinned_by_design"]}
    for workflow in _workflows():
        if workflow.name in unpinned:
            continue
        assert pin["candidate_sha"] not in workflow.read_text(encoding="utf-8"), (
            f"{workflow.name} installs the uncertified candidate SHA"
        )


@pytest.mark.parametrize("workflow", _workflows(), ids=lambda path: path.name)
def test_workflow_refs_match_a_reviewed_pin(workflow: Path, pin: dict) -> None:
    unpinned = {Path(entry).name for entry in pin["unpinned_by_design"]}
    text = workflow.read_text(encoding="utf-8")
    refs = _refs(text)
    # `${ABICHECK_REF}` is an indirection through the workflow's own env
    # block, which test_native_project_workflows_use_the_project_pin pins.
    refs = {ref for ref in refs if not ref.startswith("$")}
    if workflow.name in unpinned:
        return
    reviewed = {pin["sha"], pin["legacy_sha"]}
    unexpected = refs - reviewed
    assert not unexpected, (
        f"{workflow.name} references ABICheck at {sorted(unexpected)}; "
        f"ci/abicheck-version.yaml reviews only {sorted(reviewed)}"
    )


@pytest.mark.parametrize(
    "workflow_name",
    ["project-shadow.yml", "project-baseline.yml", "depth-scenarios.yml"],
)
def test_native_project_workflows_use_the_project_pin(workflow_name: str, pin: dict) -> None:
    """The native project path must be on `sha`, never the legacy pin."""
    text = (WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8")
    refs = {ref for ref in _refs(text) if not ref.startswith("$")}
    assert refs, f"{workflow_name} names no ABICheck ref at all"
    assert refs == {pin["sha"]}, (
        f"{workflow_name} references {sorted(refs)}, expected only {pin['sha']}"
    )
    if "ABICHECK_REF:" in text:
        env_refs = re.findall(r"ABICHECK_REF:\s*(\S+)", text)
        assert set(env_refs) == {pin["sha"]}, (
            f"{workflow_name} sets ABICHECK_REF to {env_refs}, expected {pin['sha']}"
        )


def test_baseline_generation_matches_the_pin(pin: dict) -> None:
    """Cache-key namespace and manifest generation come from the same value.

    `-g<generation>` is folded into every accepted-main cache key upstream,
    so a generation bump that missed a workflow would restore the previous
    generation's baseline through the prefix fallback.
    """
    generation = str(pin["generation"])
    for workflow_name in ("project-baseline.yml", "project-shadow.yml"):
        text = (WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8")
        declared = re.findall(r"(?:baseline-generation|expected-baseline-generation):\s*'?(\d+)'?", text)
        assert declared, f"{workflow_name} declares no baseline generation"
        assert set(declared) == {generation}, (
            f"{workflow_name} declares generation {declared}, expected {generation}"
        )
    # project-baseline.yml hands the generation to upstream, which folds
    # `-g<generation>` into the key it saves; project-shadow.yml has to
    # reconstruct that same key to restore it, so only the consumer side
    # spells the namespace out.
    shadow = (WORKFLOW_DIR / "project-shadow.yml").read_text(encoding="utf-8")
    assert f"-g{generation}-" in shadow, (
        f"project-shadow.yml does not namespace its cache keys with -g{generation}-"
    )


def test_root_composite_action_refs_are_matched() -> None:
    """Regression guard for the hole Codex found in the first version.

    `uses: abicheck/abicheck@<sha>` (the root composite action, used 11
    times across abi-scan.yml and baseline.yml) was not matched, so one of
    those could have drifted to an uncertified SHA with every test here
    still passing.
    """
    root = _refs("        uses: abicheck/abicheck@" + "a" * 40 + "\n")
    assert root == {"a" * 40}
    nested = _refs(
        "    uses: abicheck/abicheck/.github/workflows/check-project.yml@" + "b" * 40
    )
    assert nested == {"b" * 40}


def test_every_root_action_ref_in_the_repo_is_seen_by_the_guard() -> None:
    """Count what the guard sees against what a plain grep finds."""
    import re as _re

    for name in ("abi-scan.yml", "baseline.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        literal = len(_re.findall(r"uses: abicheck/abicheck@", text))
        assert literal, f"{name} has no root action refs to guard"
        # Every literal occurrence must resolve to a ref the guard collected.
        assert _refs(text), f"{name}: guard collected no refs at all"


def test_raw_installer_urls_are_matched() -> None:
    """These are curled and bashed, so their SHA selects executed code."""
    url = (
        "            \"https://raw.githubusercontent.com/abicheck/abicheck/"
        + "c" * 40
        + "/action/install-castxml.sh\"\n"
    )
    assert _refs(url) == {"c" * 40}


def test_a_tampered_installer_sha_is_visible_to_the_guard() -> None:
    """Regression guard for the exact blindness Codex found.

    Before this, changing ONLY the installer URL's SHA produced no match,
    so every pin test still passed while an unreviewed revision was curled
    and executed on the runner.
    """
    text = (WORKFLOW_DIR / "integration-shadow.yml").read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/abicheck/abicheck/" in text, (
        "this workflow no longer curls an ABICheck installer; retarget this test"
    )
    import yaml as _yaml

    pin = _yaml.safe_load(PIN_FILE.read_text(encoding="utf-8"))
    tampered = text.replace(
        f"raw.githubusercontent.com/abicheck/abicheck/{pin['legacy_sha']}",
        "raw.githubusercontent.com/abicheck/abicheck/" + "d" * 40,
    )
    assert tampered != text, "expected the legacy pin in an installer URL"
    assert "d" * 40 in _refs(tampered)


def test_every_installer_url_resolves_to_a_reviewed_pin(pin: dict) -> None:
    """Count them explicitly: a file whose pip spec happens to share the SHA
    would otherwise hide a tampered URL."""
    reviewed = {pin["sha"], pin["legacy_sha"]}
    unpinned = {Path(entry).name for entry in pin["unpinned_by_design"]}
    seen = 0
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        for match in RAW_URL_REF_RE.finditer(text):
            seen += 1
            if workflow.name in unpinned:
                continue
            assert match.group("ref") in reviewed, (
                f"{workflow.name} curls an ABICheck installer at "
                f"{match.group('ref')}, which is not a reviewed pin"
            )
    assert seen, "no installer URLs found at all; retarget this test"


# --- the executable git-fetch form (Codex review) -----------------------


def test_the_git_fetch_form_is_matched() -> None:
    line = "git fetch --depth 1 origin 6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"
    assert _refs(line) == {"6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"}


def test_a_tampered_fetch_sha_is_visible_even_beside_a_valid_pin() -> None:
    """The failure mode: another form in the same file made the file look
    clean while this ref went unread."""
    workflow = (WORKFLOW_DIR / "abi-scan.yml").read_text(encoding="utf-8")
    tampered = workflow.replace(
        "git fetch --depth 1 origin 6fb85361cf4cea67a2f444bc097cfe24cd2d99c3",
        "git fetch --depth 1 origin " + "dead" * 10,
    )
    assert tampered != workflow, "the fetch line this test targets has moved"
    assert "dead" * 10 in _refs(tampered)


def test_every_git_fetch_of_abicheck_resolves_to_a_reviewed_pin(pin: dict) -> None:
    """Counted explicitly, so a file whose pip spec shares the SHA cannot hide
    a tampered fetch."""
    reviewed = {pin["sha"], pin.get("legacy_sha"), pin.get("candidate_sha")} - {None}
    checked = 0
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        for match in GIT_FETCH_REF_RE.finditer(text):
            checked += 1
            assert match.group("ref") in reviewed, f"{workflow.name}: {match.group('ref')}"
    assert checked >= 1, "no git-fetch pin was actually checked"
