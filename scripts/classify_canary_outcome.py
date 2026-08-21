#!/usr/bin/env python3
"""Classify a canary.yml run into one of five outcomes (design doc section
24 "Canary requirements"):

    INFRASTRUCTURE_FAILED  -- a setup/environment step failed (or a later
                               step never got the chance to run) before any
                               real assertion could be made. Never a
                               detector signal.
    DETECTOR_DRIFT          -- the semantic canary (scenarios/manifest.yaml
                               run against the candidate) actually ran and
                               a scenario/suppression assertion no longer
                               holds.
    ACTION_CONTRACT_DRIFT   -- the GitHub integration canary (root Action
                               surface: collect-facts/check-target/baseline
                               resolution) actually ran and its contract
                               with this repo broke.
    BUILD_INTEGRATION_DRIFT -- the multi-build-system integration canary
                               (per-profile build + real `abicheck compare`
                               self-check) actually ran and a build-system
                               profile's ABI signal broke.
    SUCCESS                 -- every layer ran and asserted cleanly.

section 24 is explicit that infrastructure failure must never be reported
as a detector regression -- so classification here is deliberately
priority-ordered: INFRASTRUCTURE_FAILED wins over every other category,
including when a later layer *also* reports a failure (an infra failure
upstream routinely cascades into "downstream step skipped", which is
still an infrastructure story, not three separate drift stories).

Input shape: a JSON array of "signals", each
    {"name": "<human label>", "category": "infrastructure" | "semantic"
        | "action_contract" | "build_integration", "outcome": "<GitHub
        Actions step/job outcome/result string>"}
`outcome` is treated as GitHub Actions spells it: "success" is the only
outcome that counts as "ran and passed". Anything else -- "failure",
"skipped", "cancelled", "timed_out", or any other string -- is treated as
"this layer did not cleanly assert" and is bucketed per the category
rules below. A signal whose own category is "infrastructure" contributes
to INFRASTRUCTURE_FAILED on any non-success outcome; a signal in one of
the three drift categories contributes to its own drift bucket only when
its outcome is exactly "failure" (an *assertion* failed) -- any other
non-success outcome for a drift-category signal (most commonly "skipped",
because an upstream infra step failed first) is itself folded into
INFRASTRUCTURE_FAILED instead, since a step that never ran made no
assertion at all.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

VALID_CATEGORIES = ("infrastructure", "semantic", "action_contract", "build_integration")

# Doc order (section 24): the classification this function returns when
# more than one drift category has a genuine failure in the same run.
_DRIFT_PRIORITY = ("semantic", "action_contract", "build_integration")
_DRIFT_LABEL = {
    "semantic": "DETECTOR_DRIFT",
    "action_contract": "ACTION_CONTRACT_DRIFT",
    "build_integration": "BUILD_INTEGRATION_DRIFT",
}


class ClassificationError(ValueError):
    pass


def _validate_signal(signal: Any, index: int) -> Dict[str, str]:
    if not isinstance(signal, dict):
        raise ClassificationError(f"signals[{index}] must be an object, got {type(signal).__name__}")
    name = signal.get("name")
    category = signal.get("category")
    outcome = signal.get("outcome")
    if not isinstance(name, str) or not name.strip():
        raise ClassificationError(f"signals[{index}].name is required and must be a non-empty string")
    if category not in VALID_CATEGORIES:
        raise ClassificationError(
            f"signals[{index}].category must be one of {VALID_CATEGORIES} (got {category!r})"
        )
    if not isinstance(outcome, str) or not outcome.strip():
        raise ClassificationError(f"signals[{index}].outcome is required and must be a non-empty string")
    return {"name": name, "category": category, "outcome": outcome}


def classify(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not signals:
        raise ClassificationError("no signals given -- a canary run with nothing to classify is itself an error")

    validated = [_validate_signal(s, i) for i, s in enumerate(signals)]

    infra_failures = [s for s in validated if s["category"] == "infrastructure" and s["outcome"] != "success"]
    # A drift-category signal that never cleanly ran (skipped/cancelled/
    # anything but success-or-failure) is an infra story, not a drift
    # story -- fold it in here rather than double-reporting.
    cascaded = [
        s
        for s in validated
        if s["category"] != "infrastructure" and s["outcome"] not in ("success", "failure")
    ]
    infra_failures.extend(cascaded)

    if infra_failures:
        return {
            "classification": "INFRASTRUCTURE_FAILED",
            "reasons": [f"{s['name']} ({s['category']}): {s['outcome']}" for s in infra_failures],
        }

    for category in _DRIFT_PRIORITY:
        failed = [s for s in validated if s["category"] == category and s["outcome"] == "failure"]
        if failed:
            return {
                "classification": _DRIFT_LABEL[category],
                "reasons": [f"{s['name']} ({s['category']}): {s['outcome']}" for s in failed],
            }

    return {"classification": "SUCCESS", "reasons": []}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("signals_file", type=Path, help="JSON file holding the signals array (see module docstring)")
    args = parser.parse_args(argv)

    try:
        with args.signals_file.open("r", encoding="utf-8") as fh:
            signals = json.load(fh)
        if not isinstance(signals, list):
            raise ClassificationError(f"{args.signals_file}: top-level JSON must be an array, got {type(signals).__name__}")
        result = classify(signals)
    except (ClassificationError, json.JSONDecodeError, OSError) as exc:
        print(f"classify_canary_outcome: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
