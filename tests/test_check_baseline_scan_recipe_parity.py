"""Unit tests for scripts/check_baseline_scan_recipe_parity.py -- the
static field-by-field comparison of baseline.yml's canonical `dump` step
against abi-scan.yml's canonical `scan` step.

Includes a regression test run directly against this repo's own real,
current workflow files, and synthetic cases pinned to the two real bug
shapes this whole class of check exists to catch (a scanner pin drift, a
contract-defining field silently diverging between the two steps).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from check_baseline_scan_recipe_parity import ABI_SCAN_PATH, BASELINE_PATH, check

REPO_ROOT = Path(__file__).resolve().parent.parent

_GOOD_DUMP_WITH = {
    "mode": "dump",
    "new-library": "bazel-bin/libmath.so",
    "public-header-dir": "include",
    "sources": ".",
    "build-info": "${{ steps.bazel_pack.outputs.pack_dir }}",
    "depth": "source",
}
_GOOD_SCAN_WITH = {
    "mode": "scan",
    "new-library": "bazel-bin/libmath.so",
    "public-header-dir": "include",
    "sources": ".",
    "build-info": "${{ steps.bazel_pack.outputs.pack_dir }}",
    "depth": "source",
    "against": "${{ runner.temp }}/math.base.abicheck.json",
}
_SHA = "6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"


def _wf(job_id: str, step: dict, *, root_target: str = "//:math") -> dict:
    return {
        "jobs": {
            job_id: {
                "steps": [
                    {"id": "bazel_pack", "run": f'python3 x.py --root-target "{root_target}"'},
                    step,
                ]
            }
        }
    }


def _dump_wf(with_block: dict | None = None, *, uses_sha: str = _SHA, root_target: str = "//:math") -> dict:
    step = {
        "name": "Collect source-aware ABI baseline",
        "uses": f"abicheck/abicheck@{uses_sha}",
        "with": with_block if with_block is not None else dict(_GOOD_DUMP_WITH),
    }
    return _wf("collect", step, root_target=root_target)


def _scan_wf(with_block: dict | None = None, *, uses_sha: str = _SHA, root_target: str = "//:math") -> dict:
    step = {
        "id": "scan",
        "uses": f"abicheck/abicheck@{uses_sha}",
        "with": with_block if with_block is not None else dict(_GOOD_SCAN_WITH),
    }
    return _wf("scan", step, root_target=root_target)


class TestCheck:
    def test_matching_recipes_are_clean(self):
        assert check(_dump_wf(), _scan_wf()) == []

    def test_scanner_pin_drift_is_caught(self):
        errors = check(_dump_wf(), _scan_wf(uses_sha="deadbeef" * 5))
        assert any("SCANNER_PIN_MISMATCH" in e for e in errors)

    def test_public_header_dir_drift_is_caught(self):
        bad = dict(_GOOD_SCAN_WITH)
        bad["public-header-dir"] = "src"
        errors = check(_dump_wf(), _scan_wf(bad))
        assert any("RECIPE_FIELD_MISMATCH" in e and "public-header-dir" in e for e in errors)

    def test_depth_drift_is_caught(self):
        bad = dict(_GOOD_SCAN_WITH)
        bad["depth"] = "headers"
        errors = check(_dump_wf(), _scan_wf(bad))
        assert any("RECIPE_FIELD_MISMATCH" in e and "depth" in e for e in errors)

    def test_build_evidence_root_target_drift_is_caught(self):
        errors = check(_dump_wf(root_target="//:math"), _scan_wf(root_target="//:other"))
        assert any("BUILD_EVIDENCE_ROOT_TARGET_MISMATCH" in e for e in errors)

    def test_expected_differences_are_not_flagged(self):
        # mode, against, since, output-file, header/new-header spelling --
        # all legitimately differ between a dump baseline step and a scan
        # candidate step; none of these alone should produce an error.
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["header"] = "include/abicheck_lab/math.h"
        dump_with["output-file"] = "${{ runner.temp }}/math.raw.abicheck.json"
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["since"] = "${{ github.event.pull_request.base.sha }}"
        scan_with["output-file"] = "abicheck-report.json"
        assert check(_dump_wf(dump_with), _scan_wf(scan_with)) == []

    def test_an_unclassified_field_is_flagged(self):
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["some-brand-new-input"] = "x"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any("UNCLASSIFIED_FIELD" in e and "some-brand-new-input" in e for e in errors)

    def test_missing_dump_step_is_reported(self):
        errors = check({"jobs": {"collect": {"steps": []}}}, _scan_wf())
        assert len(errors) == 1
        assert "could not find" in errors[0]

    def test_missing_scan_step_is_reported(self):
        errors = check(_dump_wf(), {"jobs": {"scan": {"steps": []}}})
        assert len(errors) == 1
        assert "could not find" in errors[0]


class TestRealWorkflows:
    def test_baseline_and_abi_scan_agree_on_every_contract_field(self):
        with BASELINE_PATH.open() as f:
            baseline_wf = yaml.safe_load(f)
        with ABI_SCAN_PATH.open() as f:
            abi_scan_wf = yaml.safe_load(f)
        errors = check(baseline_wf, abi_scan_wf)
        assert errors == [], "\n".join(errors)
