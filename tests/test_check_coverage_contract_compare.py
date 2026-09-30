"""scripts/check_coverage_contract.py against a COMPARE-schema report.

Since upstream ADR-068 removed `scan`, abi-scan.yml's required gate produces
a two-sided `compare` report. tests/data/compare_report_depth_source.json is
a trimmed real report (abicheck 5ba6a5c8, `compare --depth source --since`,
locally built libmath.so) -- only keys this contract reads were kept.

Every mutation below removes or weakens exactly one piece of evidence and
must turn PASS into FAIL: the contract is fail-closed per field, not only on
the reported happy path.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from check_coverage_contract import evaluate

REPORT = json.loads(
    (Path(__file__).parent / "data" / "compare_report_depth_source.json").read_text(encoding="utf-8")
)
SUMMARY = {"compile_unit_count": 1, "resolved_target_count": 1}


def _eval(report, **kw):
    kw.setdefault("evidence_summary", SUMMARY)
    return evaluate(
        report, requested_depth="source", min_compile_units=1,
        require_bazel_target=True, require_public_header_provenance=True,
        min_export_match_ratio=0.95, **kw,
    )


def _layer(report, name):
    return next(c for c in report["layer_coverage"] if c["layer"] == name)


def test_real_compare_report_passes():
    result = _eval(REPORT)
    assert result["gate_status"] == "PASS", result["failures"]
    assert result["facts"]["effective_depth"] == "source"
    assert result["facts"]["public_header_provenance_basis"] == "scope"
    assert result["compatibility_verdict"] == REPORT["verdict"]


def _drop_assurance(r):
    r.pop("analysis_assurance")


def _null_assurance(r):
    r["analysis_assurance"] = None


def _headers_depth(r):
    r["analysis_assurance"]["effective_depth"] = "headers"


def _depth_unsatisfied(r):
    r["analysis_assurance"]["depth_satisfied"] = False


def _depth_satisfied_missing(r):
    r["analysis_assurance"].pop("depth_satisfied")


def _scope_not_applied(r):
    r["scope"]["public_headers_applied"] = False


def _scope_fell_back(r):
    r["scope"]["fell_back"] = True


def _scope_unresolved(r):
    r["scope"]["resolved"] = False


def _scope_missing(r):
    r.pop("scope")


def _l3_absent(r):
    _layer(r, "L3_build")["status"] = "skipped"


def _l4_missing(r):
    r["layer_coverage"] = [c for c in r["layer_coverage"] if c["layer"] != "L4_source_abi"]


def _l4_unparseable(r):
    _layer(r, "L4_source_abi")["detail"] = "reformatted upstream"


def _l4_low_ratio(r):
    _layer(r, "L4_source_abi")["detail"] = "scope=changed, 1/1 TUs parsed, 1/3 symbols matched, 1/3 accounted"


def _l4_unparsed_tu(r):
    _layer(r, "L4_source_abi")["detail"] = "scope=changed, 1/2 TUs parsed, 3/3 symbols matched, 3/3 accounted"


@pytest.mark.parametrize("mutate", [
    _drop_assurance, _null_assurance, _headers_depth, _depth_unsatisfied,
    _depth_satisfied_missing, _scope_not_applied, _scope_fell_back,
    _scope_unresolved, _scope_missing, _l3_absent, _l4_missing,
    _l4_unparseable, _l4_low_ratio, _l4_unparsed_tu,
])
def test_each_weakened_field_fails_closed(mutate):
    report = copy.deepcopy(REPORT)
    mutate(report)
    result = _eval(report)
    assert result["gate_status"] == "FAIL"
    assert result["compatibility_verdict"] == "NOT_FULLY_EVALUATED"


def test_missing_bazel_summary_fails_closed():
    result = _eval(REPORT, evidence_summary=None, evidence_summary_error="none")
    assert result["gate_status"] == "FAIL"


@pytest.mark.parametrize("bad", [[], None, "x", 3])
def test_non_object_report_fails_closed(bad):
    assert _eval(bad)["gate_status"] == "FAIL"
