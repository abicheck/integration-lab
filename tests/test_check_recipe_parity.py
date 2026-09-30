"""Unit tests for scripts/check_recipe_parity.py -- the static guard
against a contract-defining flag (e.g. `--public-header-dir`) being
passed through `extra-args` on an `abicheck/abicheck` Action step instead
of its typed input.

Includes a regression test pinned directly to the real bug this script
exists to catch: abi-scan.yml's `scan` step used to pass
`extra-args: '--public-header-dir include'` instead of the typed
`public-header-dir:` input, which silently produced a different
effective recipe than baseline.yml's `dump` step (already using the
typed input) -- the root cause of the recurring NOT_COMPARABLE /
`include_sequence` mismatch. That regression is asserted directly
against the real, current workflow files below, not just against a
synthetic fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from check_recipe_parity import CHECKED_WORKFLOWS, check, check_header_root_parity, check_step

REPO_ROOT = Path(__file__).resolve().parent.parent


def _workflow(jobs: dict) -> dict:
    return {"jobs": jobs}


def _step(with_block: dict, *, uses: str = "abicheck/abicheck@deadbeef", step_id: str = "scan") -> dict:
    return {"id": step_id, "uses": uses, "with": with_block}


class TestCheckStep:
    def test_extra_args_shadowing_a_flag_the_typed_input_also_sets_is_rejected(self):
        with_block = {
            "mode": "scan",
            "public-header-dir": "include",
            "extra-args": "--public-header-dir include",
        }
        errors = check_step("fake:step", with_block)
        assert len(errors) == 1
        assert "public-header-dir" in errors[0]
        assert "shadows the typed" in errors[0]

    def test_extra_args_stating_a_contract_flag_with_no_typed_input_set_is_still_rejected(self):
        # This is the actual pre-fix shape: the typed `public-header-dir`
        # input was never set at all -- the flag only ever reached the CLI
        # through extra-args -- so there's nothing to "shadow" in the
        # narrow sense, but the flag is still a contract-defining one with
        # its own typed input that should have been used instead.
        with_block = {
            "mode": "scan",
            "extra-args": "--public-header-dir include",
        }
        errors = check_step("fake:step", with_block)
        assert len(errors) == 1
        assert "public-header-dir" in errors[0]
        assert "Use the typed input instead" in errors[0]

    def test_extra_args_with_no_contract_flag_is_fine(self):
        with_block = {
            "mode": "scan",
            "extra-args": "--budget-warn-only",
        }
        assert check_step("fake:step", with_block) == []

    def test_no_extra_args_at_all_is_fine(self):
        with_block = {"mode": "scan", "public-header-dir": "include"}
        assert check_step("fake:step", with_block) == []

    def test_a_github_actions_expression_in_extra_args_is_not_mis_tokenized(self):
        # Can't be statically tokenized -- this check only reasons about
        # literal, checked-in extra-args strings, not runtime expressions.
        with_block = {"mode": "scan", "extra-args": "${{ inputs.extra }}"}
        assert check_step("fake:step", with_block) == []

    def test_a_static_flag_next_to_an_expression_is_still_caught(self):
        # A GHA expression's *value* can't be tokenized statically, but a
        # literal, statically-visible flag on the same extra-args line must
        # not be masked away along with it -- only the expression span
        # itself is opaque.
        with_block = {
            "mode": "scan",
            "extra-args": '--public-header-dir "${{ inputs.public_dir }}"',
        }
        errors = check_step("fake:step", with_block)
        assert len(errors) == 1
        assert "public-header-dir" in errors[0]


class TestCheckWorkflow:
    def test_flags_the_known_bad_shape(self):
        workflow = _workflow(
            {
                "scan": {
                    "steps": [
                        _step(
                            {
                                "mode": "scan",
                                "new-header": "include/abicheck_lab/math.h",
                                "extra-args": "--public-header-dir include",
                            }
                        )
                    ]
                }
            }
        )
        errors = check({Path("fake.yml"): workflow})
        assert len(errors) == 1
        assert "fake.yml" in errors[0]

    def test_typed_input_alone_is_clean(self):
        workflow = _workflow(
            {
                "scan": {
                    "steps": [
                        _step(
                            {
                                "mode": "scan",
                                "new-header": "include/abicheck_lab/math.h",
                                "public-header-dir": "include",
                            }
                        )
                    ]
                }
            }
        )
        assert check({Path("fake.yml"): workflow}) == []

    def test_non_abicheck_steps_are_ignored(self):
        workflow = _workflow(
            {
                "scan": {
                    "steps": [
                        {
                            "uses": "actions/checkout@deadbeef",
                            "with": {"extra-args": "--public-header-dir include"},
                        }
                    ]
                }
            }
        )
        assert check({Path("fake.yml"): workflow}) == []


class TestRealWorkflows:
    """The actual regression coverage: run the checker against this
    repo's own, real, currently-checked-in workflow files."""

    def test_checked_workflows_list_matches_files_that_exist_on_disk(self):
        # Guards the static CHECKED_WORKFLOWS list itself against drift --
        # a typo'd or removed path here would silently stop checking that
        # file, the same "landed unexercised" gap this repo's other
        # check_*.py scripts already guard against for their own inputs.
        for path in CHECKED_WORKFLOWS:
            assert path.parent == REPO_ROOT / ".github" / "workflows"

    @pytest.mark.parametrize("path", CHECKED_WORKFLOWS, ids=lambda p: p.name)
    def test_workflow_has_no_shadowed_contract_flags(self, path):
        if not path.exists():
            pytest.skip(f"{path} does not exist in this checkout")
        with path.open() as f:
            workflow = yaml.safe_load(f)
        errors = check({path: workflow})
        assert errors == [], "\n".join(errors)


class TestHeaderRootParity:
    """The scan -> compare migration's parity rule: a two-sided source-depth
    compare against the trusted math baseline must see exactly the baseline
    dump's `public-header-dir` as its `new-header`, and nothing else."""

    BASELINE = Path("baseline.yml")
    GATE = Path("abi-scan.yml")
    BASE = "${{ runner.temp }}/math.base.abicheck.json"

    def _baseline(self, public_dir="include"):
        return _workflow({"collect": {"steps": [_step({
            "mode": "dump", "new-library": "bazel-bin/libmath.so",
            "public-header-dir": public_dir, "depth": "source",
        })]}})

    def _gate(self, **with_extra):
        block = {"mode": "compare", "old-library": self.BASE,
                 "new-library": "bazel-bin/libmath.so", "depth": "source"}
        block.update(with_extra)
        return _workflow({"scan": {"steps": [_step(block)]}})

    @pytest.mark.parametrize("public_dir", ["include", "include/abicheck_lab", "hdrs"])
    def test_matching_directory_root_is_clean(self, public_dir):
        errors = check_header_root_parity({
            self.BASELINE: self._baseline(public_dir),
            self.GATE: self._gate(**{"new-header": public_dir}),
        })
        assert errors == []

    @pytest.mark.parametrize("new_header", [
        "include/abicheck_lab/math.h",   # a file root instead of the directory
        "include/abicheck_lab",          # a narrower directory
        "include include/abicheck_lab/math.h",  # directory + an extra file root
        None,                            # no header root at all
    ])
    def test_any_other_candidate_root_is_rejected(self, new_header):
        extra = {} if new_header is None else {"new-header": new_header}
        errors = check_header_root_parity({
            self.BASELINE: self._baseline("include"),
            self.GATE: self._gate(**extra),
        })
        assert len(errors) == 1 and "new-header" in errors[0]

    @pytest.mark.parametrize("forbidden", ["header", "public-header-dir"])
    def test_both_sided_or_dump_only_header_inputs_are_rejected(self, forbidden):
        errors = check_header_root_parity({
            self.BASELINE: self._baseline(),
            self.GATE: self._gate(**{"new-header": "include", forbidden: "include"}),
        })
        assert any(forbidden in e for e in errors)

    def test_retired_scan_mode_is_rejected_anywhere(self):
        wf = _workflow({"x": {"steps": [_step({"mode": "scan", "new-library": "a.so"})]}})
        errors = check_header_root_parity({Path("other.yml"): wf})
        assert len(errors) == 1 and "mode: scan" in errors[0]

    def test_no_gate_step_at_all_fails_closed(self):
        errors = check_header_root_parity({self.BASELINE: self._baseline()})
        assert any("nothing to check" in e for e in errors)

    def test_real_workflows_satisfy_parity(self):
        workflows = {}
        for path in CHECKED_WORKFLOWS:
            with path.open() as f:
                workflows[path] = yaml.safe_load(f)
        assert check_header_root_parity(workflows) == []
