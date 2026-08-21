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

_CQUERY_CMD = "bazel cquery --output=jsonproto 'deps(//:math)'"
_AQUERY_CMD = "bazel aquery --output=jsonproto --include_param_files \"mnemonic('CppCompile', deps(//:math))\""
_PIP_LINE = f'pip install --quiet "abicheck @ git+https://github.com/abicheck/abicheck.git@{_SHA}"'


def _bazel_queries_run(*, cquery: str = _CQUERY_CMD, aquery: str = _AQUERY_CMD, extra: str = "") -> str:
    return f'{cquery} > "$RUNNER_TEMP/bazel-cquery.json"\n{aquery} > "$RUNNER_TEMP/bazel-aquery.json"\n{extra}'


def _wf(
    job_id: str,
    step: dict,
    *,
    root_target: str = "//:math",
    bazel_queries_run: str | None = None,
    abicheck_pip_run: str | None = None,
) -> dict:
    return {
        "jobs": {
            job_id: {
                "steps": [
                    {"id": "bazel_queries", "run": bazel_queries_run if bazel_queries_run is not None else _bazel_queries_run()},
                    {"id": "abicheck_pip", "run": abicheck_pip_run if abicheck_pip_run is not None else _PIP_LINE},
                    {"id": "bazel_pack", "run": f'python3 x.py --root-target "{root_target}"'},
                    step,
                ]
            }
        }
    }


def _dump_wf(
    with_block: dict | None = None,
    *,
    uses_sha: str = _SHA,
    root_target: str = "//:math",
    bazel_queries_run: str | None = None,
    abicheck_pip_run: str | None = None,
) -> dict:
    step = {
        "name": "Collect source-aware ABI baseline",
        "uses": f"abicheck/abicheck@{uses_sha}",
        "with": with_block if with_block is not None else dict(_GOOD_DUMP_WITH),
    }
    return _wf("collect", step, root_target=root_target, bazel_queries_run=bazel_queries_run, abicheck_pip_run=abicheck_pip_run)


def _scan_wf(
    with_block: dict | None = None,
    *,
    uses_sha: str = _SHA,
    root_target: str = "//:math",
    bazel_queries_run: str | None = None,
    abicheck_pip_run: str | None = None,
) -> dict:
    step = {
        "id": "scan",
        "uses": f"abicheck/abicheck@{uses_sha}",
        "with": with_block if with_block is not None else dict(_GOOD_SCAN_WITH),
    }
    # abi-scan.yml's own real bazel_queries step legitimately has one extra
    # trailing `bazel query 'buildfiles(...)'` line beyond baseline.yml's --
    # default this synthetic scan side to carry the identical asymmetry, so
    # the "clean" default case exercises that real shape rather than a
    # simplified one that would never catch the check ignoring it correctly.
    if bazel_queries_run is None:
        bazel_queries_run = _bazel_queries_run(extra="bazel query 'buildfiles(deps(//:math))' > \"$RUNNER_TEMP/bazel-buildfiles.txt\"")
    return _wf("scan", step, root_target=root_target, bazel_queries_run=bazel_queries_run, abicheck_pip_run=abicheck_pip_run)


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
        # mode, against, since, output-file -- all legitimately differ
        # between a dump baseline step and a scan candidate step; none of
        # these alone should produce an error. header/new-header spelling
        # is *not* an unconditional pass -- see the dedicated header tests
        # below -- but agreeing on the same (here: unset) value is fine.
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["output-file"] = "${{ runner.temp }}/math.raw.abicheck.json"
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["since"] = "${{ github.event.pull_request.base.sha }}"
        scan_with["output-file"] = "abicheck-report.json"
        assert check(_dump_wf(dump_with), _scan_wf(scan_with)) == []

    def test_header_and_new_header_agreeing_on_the_same_value_is_fine(self):
        # A real, if currently unused, legitimate shape: both sides name
        # the identical explicit header path, just spelled through their
        # own side's typed input (dump's `header`, scan's `new-header`).
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["header"] = "include/abicheck_lab/math.h"
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["new-header"] = "include/abicheck_lab/math.h"
        assert check(_dump_wf(dump_with), _scan_wf(scan_with)) == []

    def test_a_one_sided_new_header_is_caught(self):
        # The exact regression a prior revision of this script would have
        # missed (Codex review): re-adding a redundant new-header only to
        # the scan step, with the dump step's header left unset, recreates
        # the include_sequence/NOT_COMPARABLE bug this repo is named for.
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["new-header"] = "include/abicheck_lab/math.h"
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any("RECIPE_FIELD_MISMATCH" in e and "header" in e for e in errors)

    def test_differing_header_values_are_caught(self):
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["header"] = "include/abicheck_lab/math.h"
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["new-header"] = "include/abicheck_lab/other.h"
        errors = check(_dump_wf(dump_with), _scan_wf(scan_with))
        assert any("RECIPE_FIELD_MISMATCH" in e and "header" in e for e in errors)

    def test_scan_step_setting_generic_header_instead_of_new_header_is_caught(self):
        # Fresh evidence (Codex review): the Action still forwards a plain
        # `header:` on the scan step as a real -H root, changing scan's
        # effective header roots exactly like a redundant `new-header:`
        # would -- but silently, since the normalized comparison above
        # only ever reads scan_with["new-header"]. Rejected outright,
        # independent of value.
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["header"] = "include/abicheck_lab/math.h"
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any(str(ABI_SCAN_PATH) in e and "header" in e for e in errors)

    def test_dump_step_setting_new_header_instead_of_header_is_caught(self):
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["new-header"] = "include/abicheck_lab/math.h"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any(str(BASELINE_PATH) in e and "new-header" in e for e in errors)

    def test_mode_pair_is_asserted_exactly(self):
        # A bare inequality check would accept baseline.yml's collector
        # being accidentally changed to `mode: scan` -- baseline.yml
        # doesn't run on pull requests, so this static check is the only
        # thing that would catch it before it broke the real baseline job
        # on the next push to main (Codex review, fresh evidence).
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["mode"] = "scan"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any("MODE_MISMATCH" in e for e in errors)

    def test_old_header_on_either_canonical_step_is_flagged(self):
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["old-header"] = "include/abicheck_lab/math.h"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any("old-header" in e for e in errors)

    def test_an_unclassified_field_is_flagged(self):
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["some-brand-new-input"] = "x"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any("UNCLASSIFIED_FIELD" in e and "some-brand-new-input" in e for e in errors)

    def test_evidence_pack_cquery_drift_is_caught(self):
        # The exact shape the check exists for: a `with:` block that agrees
        # on every typed input, but the *evidence* feeding both steps was
        # collected with two different bazel cquery commands.
        drifted = _bazel_queries_run(cquery="bazel cquery --output=jsonproto 'deps(//:other)'")
        errors = check(_dump_wf(), _scan_wf(bazel_queries_run=drifted))
        assert any("BUILD_EVIDENCE_QUERY_MISMATCH" in e and "cquery" in e for e in errors)

    def test_evidence_pack_aquery_drift_is_caught(self):
        drifted = _bazel_queries_run(aquery="bazel aquery --output=jsonproto \"mnemonic('CppLink', deps(//:math))\"")
        errors = check(_dump_wf(), _scan_wf(bazel_queries_run=drifted))
        assert any("BUILD_EVIDENCE_QUERY_MISMATCH" in e and "aquery" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_is_caught(self):
        # A second, independent abicheck pin (the one that *runs*
        # build_bazel_evidence_pack.py) drifting from the scanner Action's
        # own pin, with the `uses:` pins on both canonical steps unchanged.
        drifted = 'pip install --quiet "abicheck @ git+https://github.com/abicheck/abicheck.git@deadbeef0000000000000000000000000000000"'
        errors = check(_dump_wf(), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_bazel_pack_script_drift_is_caught(self):
        # A structural difference in the bazel_pack invocation itself
        # (beyond --root-target, which is checked separately) -- e.g. one
        # side passing an extra flag the other doesn't.
        step = {
            "id": "scan",
            "uses": f"abicheck/abicheck@{_SHA}",
            "with": dict(_GOOD_SCAN_WITH),
        }
        wf = _wf("scan", step)
        wf["jobs"]["scan"]["steps"][2]["run"] = 'python3 x.py --root-target "//:math" --extra-flag'
        errors = check(_dump_wf(), wf)
        assert any("BUILD_EVIDENCE_PACK_SCRIPT_MISMATCH" in e for e in errors)

    def test_evidence_pack_queries_agreeing_is_clean(self):
        assert check(_dump_wf(), _scan_wf()) == []

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
