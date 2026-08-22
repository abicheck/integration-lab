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
    "fail-on-breaking": True,
    "fail-on-api-break": True,
}
_SHA = "6fb85361cf4cea67a2f444bc097cfe24cd2d99c3"

_CQUERY_CMD = "bazel cquery --output=jsonproto 'deps(//:math)'"
_AQUERY_CMD = "bazel aquery --output=jsonproto --include_param_files \"mnemonic('CppCompile', deps(//:math))\""
_PIP_LINE = f'pip install --quiet "abicheck @ git+https://github.com/abicheck/abicheck.git@{_SHA}"'
_BUILD_MATH_RUN = 'bazel build //:math --disk_cache="$HOME/.cache/bazel-disk"'


def _bazel_queries_run(*, cquery: str = _CQUERY_CMD, aquery: str = _AQUERY_CMD, extra: str = "") -> str:
    return f'{cquery} > "$RUNNER_TEMP/bazel-cquery.json"\n{aquery} > "$RUNNER_TEMP/bazel-aquery.json"\n{extra}'


def _set_step_run(wf: dict, job_id: str, step_id: str, run: str) -> None:
    """Mutates `wf` in place, setting the `run:` text of the job's step
    with the given `id`. Looking steps up by id (rather than a positional
    index into the steps list) keeps every test resilient to the list's
    own ordering, since `_wf` is free to insert/reorder steps over time."""
    for step in wf["jobs"][job_id]["steps"]:
        if step.get("id") == step_id:
            step["run"] = run
            return
    raise AssertionError(f"no step with id={step_id!r} in job {job_id!r}")


def _wf(
    job_id: str,
    step: dict,
    *,
    root_target: str = "//:math",
    bazel_build_run: str | None = None,
    bazel_queries_run: str | None = None,
    abicheck_pip_run: str | None = None,
) -> dict:
    return {
        "jobs": {
            job_id: {
                "steps": [
                    # No `id:` -- mirrors the real workflows' own
                    # unnamed "Build [candidate/shared library] with
                    # Bazel" steps, found by `run:` text alone.
                    {"name": "Build shared library with Bazel", "run": bazel_build_run if bazel_build_run is not None else _BUILD_MATH_RUN},
                    {"id": "bazel_queries", "run": bazel_queries_run if bazel_queries_run is not None else _bazel_queries_run()},
                    {"id": "abicheck_pip", "run": abicheck_pip_run if abicheck_pip_run is not None else _PIP_LINE},
                    {
                        "id": "bazel_pack",
                        "run": (
                            f'python3 x.py --cquery "$RUNNER_TEMP/bazel-cquery.json" '
                            f'--aquery "$RUNNER_TEMP/bazel-aquery.json" --root-target "{root_target}"'
                        ),
                    },
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
    bazel_build_run: str | None = None,
    bazel_queries_run: str | None = None,
    abicheck_pip_run: str | None = None,
) -> dict:
    step = {
        "name": "Collect source-aware ABI baseline",
        "uses": f"abicheck/abicheck@{uses_sha}",
        "with": with_block if with_block is not None else dict(_GOOD_DUMP_WITH),
    }
    return _wf(
        "collect",
        step,
        root_target=root_target,
        bazel_build_run=bazel_build_run,
        bazel_queries_run=bazel_queries_run,
        abicheck_pip_run=abicheck_pip_run,
    )


def _scan_wf(
    with_block: dict | None = None,
    *,
    uses_sha: str = _SHA,
    root_target: str = "//:math",
    bazel_build_run: str | None = None,
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
    return _wf(
        "scan",
        step,
        root_target=root_target,
        bazel_build_run=bazel_build_run,
        bazel_queries_run=bazel_queries_run,
        abicheck_pip_run=abicheck_pip_run,
    )


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

    def test_scan_step_repointed_at_an_untrusted_against_path_is_caught(self):
        # `against` was previously blanket-exempted as "expected to
        # differ", which would let this static gate accept a canonical
        # scan step silently repointed at any other baseline -- including
        # the repo-committed abi/math.abicheck.json, a working-tree file
        # the same PR can edit, defeating the trusted-baseline invariant
        # (Codex review, fresh evidence, P1). It must be exactly the
        # resolve-baseline-produced path.
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["against"] = "abi/math.abicheck.json"
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any("AGAINST_MISMATCH" in e for e in errors)

    def test_scan_step_with_fail_on_breaking_disabled_is_caught(self):
        # `Enforce gate` only fails the required PR check when
        # steps.scan.outcome == 'failure', and the Action itself only
        # exits non-zero on a BREAKING verdict when fail-on-breaking is
        # true -- blanket-exempting this flag would let the canonical
        # scan step be silently changed to fail-on-breaking: false,
        # leaving a real ABI break green (Codex review, fresh evidence,
        # P1).
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["fail-on-breaking"] = False
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any("SCAN_GATE_FLAG_MISMATCH" in e and "fail-on-breaking" in e for e in errors)

    def test_scan_step_with_fail_on_api_break_disabled_is_caught(self):
        scan_with = dict(_GOOD_SCAN_WITH)
        scan_with["fail-on-api-break"] = False
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any("SCAN_GATE_FLAG_MISMATCH" in e and "fail-on-api-break" in e for e in errors)

    def test_scan_step_with_fail_on_breaking_unset_is_caught(self):
        # Absence must be caught too, not just an explicit `false`.
        scan_with = dict(_GOOD_SCAN_WITH)
        del scan_with["fail-on-breaking"]
        errors = check(_dump_wf(), _scan_wf(scan_with))
        assert any("SCAN_GATE_FLAG_MISMATCH" in e and "fail-on-breaking" in e for e in errors)

    def test_dump_step_fail_on_breaking_value_is_unconstrained(self):
        # Dump mode has nothing to gate on -- the dump step's own value
        # (true, false, or unset) must never affect this check.
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["fail-on-breaking"] = False
        assert check(_dump_wf(dump_with), _scan_wf()) == []

    def test_dump_step_setting_against_is_caught(self):
        # Dump mode never compares against anything -- an `against` on the
        # dump step is itself a usage-shape drift, independent of the scan
        # side's own value.
        dump_with = dict(_GOOD_DUMP_WITH)
        dump_with["against"] = "${{ runner.temp }}/math.base.abicheck.json"
        errors = check(_dump_wf(dump_with), _scan_wf())
        assert any("AGAINST_MISMATCH" in e for e in errors)

    def test_scan_step_against_the_trusted_path_is_clean(self):
        assert check(_dump_wf(), _scan_wf()) == []

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

    def test_a_diagnostic_query_of_the_same_kind_writing_a_different_file_is_ignored(self):
        # Fresh evidence (Codex review): a step can legitimately contain
        # more than one `cquery`/`aquery` invocation of the same kind --
        # e.g. an extra diagnostic query -- writing to a DIFFERENT file
        # than the one bazel_pack actually consumes. Selecting "the last
        # invocation of that kind" rather than "the one whose redirect
        # matches bazel_pack's own --cquery/--aquery input" would let a
        # drifted diagnostic query silently become "the" compared command.
        # The real evidence-defining query here is unchanged and must
        # still be recognized as matching.
        extra_diagnostic = (
            "bazel cquery --output=jsonproto 'deps(//:other)' > \"$RUNNER_TEMP/bazel-cquery-diagnostic.json\""
        )
        drifted = _bazel_queries_run(extra=extra_diagnostic)
        errors = check(_dump_wf(), _scan_wf(bazel_queries_run=drifted))
        assert not any("BUILD_EVIDENCE_QUERY_MISMATCH" in e for e in errors)
        assert not any("could not find a 'bazel cquery' command" in e for e in errors)

    def test_a_drifted_query_of_the_same_kind_writing_the_consumed_file_is_still_caught(self):
        # The converse of the case above: if the invocation that redirects
        # to the path bazel_pack actually reads IS the drifted one (an
        # earlier, unchanged query writes an unrelated diagnostic file
        # instead), the mismatch must still be caught.
        harmless_diagnostic = (
            "bazel cquery --output=jsonproto 'deps(//:math)' > \"$RUNNER_TEMP/bazel-cquery-diagnostic.json\""
        )
        drifted_real_query = "bazel cquery --output=jsonproto 'deps(//:other)' > \"$RUNNER_TEMP/bazel-cquery.json\""
        drifted = (
            f"{harmless_diagnostic}\n{drifted_real_query}\n{_AQUERY_CMD} > \"$RUNNER_TEMP/bazel-aquery.json\""
        )
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

    def test_evidence_pack_pip_pin_drift_behind_an_identical_preliminary_command_is_caught(self):
        # The exact regression a prior revision of the pip-pin regex would
        # have missed (Codex review, fresh evidence): if both sides gain an
        # identical preliminary command (e.g. `pip install wheel`) ahead of
        # the real abicheck install, a first-match search on `pip install`
        # would keep comparing that identical preliminary line while the
        # real, differing abicheck pin went unchecked.
        good = f"pip install --quiet wheel\n{_PIP_LINE}"
        drifted = (
            'pip install --quiet wheel\n'
            'pip install --quiet "abicheck @ git+https://github.com/abicheck/abicheck.git@deadbeef0000000000000000000000000000000"'
        )
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_in_a_second_reinstall_is_caught(self):
        # Fresh evidence beyond the preliminary-command fix above (Codex
        # review): a step with TWO abicheck installs on separate lines --
        # both sides share the identical first install, but one side later
        # reinstalls a different ref. pip install reinstalls/overwrites, so
        # the LATER ref is the one that's actually installed and runs
        # build_bazel_evidence_pack.py; a first-match search would keep
        # comparing the shared, stale first install and miss this entirely.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        drifted_second_install = (
            'pip install --quiet "abicheck @ git+https://github.com/abicheck/abicheck.git@deadbeef0000000000000000000000000000000"'
        )
        drifted = f"{shared_first_install}\n{drifted_second_install}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_via_bare_vcs_url_reinstall_is_caught(self):
        # Fresh evidence beyond the PEP-508-only pattern (Codex review):
        # `pip install <vcs project url>` (no `package @ ` prefix at all)
        # is an equally real, pip-documented spelling. A later bare-URL
        # reinstall using this spelling must still be recognized as the
        # effective pin, not silently ignored because it doesn't match the
        # `abicheck @ git+...` form.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        bare_url_reinstall = (
            "pip install --force-reinstall "
            "git+https://github.com/abicheck/abicheck.git@deadbeef0000000000000000000000000000000"
        )
        drifted = f"{shared_first_install}\n{bare_url_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
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
        _set_step_run(wf, "scan", "bazel_pack", 'python3 x.py --root-target "//:math" --extra-flag')
        errors = check(_dump_wf(), wf)
        assert any("BUILD_EVIDENCE_PACK_SCRIPT_MISMATCH" in e for e in errors)

    def test_artifact_build_command_drift_is_caught(self):
        # The parity check compared how the evidence pack was collected
        # and how the two Action steps were invoked, but never the Bazel
        # command that actually produces bazel-bin/libmath.so -- a
        # candidate build silently gaining an ABI-affecting flag (e.g.
        # --config=asan) still produces the same output path and would
        # pass every other check here (Codex review, fresh evidence).
        drifted = 'bazel build //:math --config=asan --disk_cache="$HOME/.cache/bazel-disk"'
        errors = check(_dump_wf(), _scan_wf(bazel_build_run=drifted))
        assert any("BUILD_EVIDENCE_ARTIFACT_BUILD_MISMATCH" in e for e in errors)

    def test_matching_artifact_build_commands_is_clean(self):
        assert check(_dump_wf(), _scan_wf()) == []

    def test_artifact_build_env_drift_is_caught(self):
        # Fresh evidence (Codex review): a build step silently gaining a
        # toolchain-selecting env var (CC/CXX) leaves the `run:` text
        # identical, so only comparing the command string would miss it.
        # abi-scan.yml's own L4-replay diagnostic rebuild already sets
        # exactly this shape (env: {CC: clang-18, CXX: clang++-18}) on
        # the identical command, elsewhere in the same job.
        scan_wf = _scan_wf()
        for step in scan_wf["jobs"]["scan"]["steps"]:
            if str(step.get("run", "")).startswith("bazel build //:math"):
                step["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
                break
        errors = check(_dump_wf(), scan_wf)
        assert any("BUILD_EVIDENCE_ARTIFACT_BUILD_ENV_MISMATCH" in e for e in errors)

    def test_matching_artifact_build_env_is_clean(self):
        dump_wf = _dump_wf()
        scan_wf = _scan_wf()
        for wf, job_id in ((dump_wf, "collect"), (scan_wf, "scan")):
            for step in wf["jobs"][job_id]["steps"]:
                if str(step.get("run", "")).startswith("bazel build //:math"):
                    step["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
                    break
        assert check(dump_wf, scan_wf) == []

    def test_job_level_env_drift_is_caught(self):
        # Fresh evidence (Codex review): a `run` step also inherits
        # workflow- and job-level env vars, not just its own -- a
        # toolchain-selecting CC/CXX set at jobs.scan.env alone (never on
        # the step itself) must still be caught.
        scan_wf = _scan_wf()
        scan_wf["jobs"]["scan"]["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
        errors = check(_dump_wf(), scan_wf)
        assert any("BUILD_EVIDENCE_ARTIFACT_BUILD_ENV_MISMATCH" in e for e in errors)

    def test_workflow_level_env_drift_is_caught(self):
        scan_wf = _scan_wf()
        scan_wf["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
        errors = check(_dump_wf(), scan_wf)
        assert any("BUILD_EVIDENCE_ARTIFACT_BUILD_ENV_MISMATCH" in e for e in errors)

    def test_matching_job_level_env_is_clean(self):
        dump_wf = _dump_wf()
        scan_wf = _scan_wf()
        dump_wf["jobs"]["collect"]["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
        scan_wf["jobs"]["scan"]["env"] = {"CC": "clang-18", "CXX": "clang++-18"}
        assert check(dump_wf, scan_wf) == []

    def test_missing_artifact_build_step_is_reported(self):
        dump_wf = _dump_wf()
        dump_wf["jobs"]["collect"]["steps"] = [
            s for s in dump_wf["jobs"]["collect"]["steps"] if not str(s.get("run", "")).startswith("bazel build //:math")
        ]
        errors = check(dump_wf, _scan_wf())
        assert any("could not find a 'bazel build //:math' step in job 'collect'" in e for e in errors)

    def test_evidence_pack_queries_agreeing_is_clean(self):
        assert check(_dump_wf(), _scan_wf()) == []

    def test_a_later_repeated_root_target_drift_is_caught(self):
        # --root-target is documented and implemented (action="append")
        # as repeatable -- comparing only the first occurrence would
        # silently ignore a drift in any later one, even though both
        # workflows' evidence pack is scoped to the FULL set of roots
        # passed (Codex review, fresh evidence).
        dump_wf = _dump_wf()
        scan_wf = _scan_wf()
        base_flags = (
            'python3 x.py --cquery "$RUNNER_TEMP/bazel-cquery.json" '
            '--aquery "$RUNNER_TEMP/bazel-aquery.json" --root-target "//:math"'
        )
        _set_step_run(dump_wf, "collect", "bazel_pack", f'{base_flags} --root-target "//:extra"')
        _set_step_run(scan_wf, "scan", "bazel_pack", f'{base_flags} --root-target "//:different"')
        errors = check(dump_wf, scan_wf)
        assert any("BUILD_EVIDENCE_ROOT_TARGET_MISMATCH" in e for e in errors)

    def test_multiple_matching_root_targets_is_clean(self):
        dump_wf = _dump_wf()
        scan_wf = _scan_wf()
        run = (
            'python3 x.py --cquery "$RUNNER_TEMP/bazel-cquery.json" '
            '--aquery "$RUNNER_TEMP/bazel-aquery.json" '
            '--root-target "//:math" --root-target "//:extra"'
        )
        _set_step_run(dump_wf, "collect", "bazel_pack", run)
        _set_step_run(scan_wf, "scan", "bazel_pack", run)
        assert check(dump_wf, scan_wf) == []

    def test_evidence_pack_pip_pin_drift_via_named_ref_reinstall_is_caught(self):
        # A second round on the same finding (Codex review, fresh
        # evidence): pip accepts a named ref (`@main`) or no `@ref` suffix
        # at all just as validly as a pinned SHA. A prior revision of the
        # regex required `@[0-9a-f]{7,40}`, which silently ignored a later
        # reinstall onto a moving ref.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        named_ref_reinstall = "pip install --force-reinstall git+https://github.com/abicheck/abicheck.git@main"
        drifted = f"{shared_first_install}\n{named_ref_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_via_no_ref_reinstall_is_caught(self):
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        no_ref_reinstall = "pip install --force-reinstall git+https://github.com/abicheck/abicheck.git"
        drifted = f"{shared_first_install}\n{no_ref_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_via_line_continued_reinstall_is_caught(self):
        # Fresh evidence (Codex review): a real reinstall is often spelled
        # with an ordinary shell line continuation. Without DOTALL, `.`
        # cannot cross that newline, so the whole continued command was
        # invisible to this pattern and a reinstall onto a different ref
        # went unrecognized entirely.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        continued_reinstall = (
            "pip install --force-reinstall \\\n"
            '  "abicheck @ git+https://github.com/abicheck/abicheck.git@'
            'deadbeef0000000000000000000000000000000"'
        )
        drifted = f"{shared_first_install}\n{continued_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_via_index_reinstall_is_caught(self):
        # Fresh evidence (Codex review): pip accepts a plain requirement
        # specifier naming the package on an index just as validly as a
        # VCS URL -- `_PIP_VCS_INSTALL_RE` requires `git+`, so a later
        # reinstall from PyPI/an index went completely unmatched and
        # `_abicheck_pip_pin` kept comparing the stale shared VCS install.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        index_reinstall = "pip install --force-reinstall abicheck==1.2.3"
        drifted = f"{shared_first_install}\n{index_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_evidence_pack_pip_pin_drift_via_bare_index_reinstall_is_caught(self):
        # No version specifier at all -- still a real, valid install.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        index_reinstall = "pip install --force-reinstall abicheck"
        drifted = f"{shared_first_install}\n{index_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_a_shared_vcs_install_with_no_index_reinstall_is_clean(self):
        # The bare-requirement pattern must not spuriously match inside a
        # genuine, unchanged VCS install line -- confirms it doesn't fire
        # on the "abicheck" tokens already present in a PEP 508 VCS URL.
        assert check(_dump_wf(), _scan_wf()) == []

    def test_evidence_pack_pip_pin_drift_via_extras_reinstall_is_caught(self):
        # Fresh evidence (Codex review): pip's requirement-specifier form
        # also accepts an "extras" segment (`abicheck[foo]==1.2.3`) -- a
        # real PEP 508 shape. Without capturing it, the match truncated at
        # the bare `abicheck`, so two installs differing only in extras
        # and version normalized to the identical text and a real
        # producer-version drift went undetected.
        shared_first_install = _PIP_LINE
        good = f"{shared_first_install}\n{shared_first_install}"
        extras_reinstall = "pip install --force-reinstall abicheck[foo]==1.2.3"
        drifted = f"{shared_first_install}\n{extras_reinstall}"
        errors = check(_dump_wf(abicheck_pip_run=good), _scan_wf(abicheck_pip_run=drifted))
        assert any("BUILD_EVIDENCE_PIP_PIN_MISMATCH" in e for e in errors)

    def test_matching_extras_reinstalls_is_clean(self):
        extras_install = "pip install --force-reinstall abicheck[foo]==1.2.3"
        assert check(_dump_wf(abicheck_pip_run=extras_install), _scan_wf(abicheck_pip_run=extras_install)) == []

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
