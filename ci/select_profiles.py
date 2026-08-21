#!/usr/bin/env python3
"""Given an event kind, resolve ci/profiles.yaml + ci/event-policy.yaml
into the matrix-ready list of profile ids that event should run (PR1 item
8). Printed as JSON on stdout so a GitHub Actions job can consume it
directly via `fromJson()`:

    python3 ci/select_profiles.py --event pull_request
    {"event": "pull_request", "required": [], "advisory": ["linux-x86_64-gcc14-cxx17-bazel", ...], "profiles": [...]}

`profiles` is the union of `required` + `advisory`, de-duplicated and
sorted -- the field a workflow's own matrix `include`/`strategy.matrix`
almost always wants directly. `--profile <id>` (repeatable) narrows the
result to just those ids (still validated against profiles.yaml), for a
manual/workflow_dispatch run that wants to test one profile at a time.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

CI_DIR = Path(__file__).resolve().parent
REPO_ROOT = CI_DIR.parent
DEFAULT_PROFILES = CI_DIR / "profiles.yaml"
DEFAULT_POLICY = CI_DIR / "event-policy.yaml"


class SelectionError(ValueError):
    pass


def _load_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_profiles(path: Path = DEFAULT_PROFILES) -> Dict[str, Dict[str, Any]]:
    doc = _load_yaml(path)
    profiles: Dict[str, Dict[str, Any]] = {}
    for entry in doc.get("profiles", []):
        # A duplicate id would otherwise silently overwrite the earlier
        # entry in the dict comprehension this replaced, dropping a
        # profile from every matrix/staging run with no error; a missing
        # id raised an unhandled KeyError instead of the intended
        # SelectionError (CodeRabbit review, PR #19).
        profile_id = entry.get("id")
        if not profile_id:
            raise SelectionError(f"{path}: profile entry missing a non-empty 'id': {entry!r}")
        if profile_id in profiles:
            raise SelectionError(f"{path}: duplicate profile id {profile_id!r}")
        profiles[profile_id] = entry
    if not profiles:
        raise SelectionError(f"{path}: no profiles declared")
    return profiles


def load_policy(path: Path = DEFAULT_POLICY) -> Dict[str, Any]:
    return _load_yaml(path)


def _expand(names: List[str], profile_sets: Dict[str, List[str]], known_ids: set) -> List[str]:
    """A name in an events.<event>.required/advisory list is either a
    literal profile id or a profile_sets key -- expand and validate every
    entry, failing closed on an unknown reference rather than silently
    dropping it (a typo'd profile id/set name must be visible, not
    silently produce an empty/short matrix).
    """
    resolved: List[str] = []
    for name in names:
        if name in profile_sets:
            for member in profile_sets[name]:
                if member not in known_ids:
                    raise SelectionError(
                        f"profile_sets[{name!r}] references unknown profile id: {member!r}"
                    )
                resolved.append(member)
        elif name in known_ids:
            resolved.append(name)
        else:
            raise SelectionError(f"unknown profile id or profile_sets entry: {name!r}")
    return resolved


def select(event: str, profiles_path: Path = DEFAULT_PROFILES, policy_path: Path = DEFAULT_POLICY) -> Dict[str, Any]:
    profiles = load_profiles(profiles_path)
    policy = load_policy(policy_path)

    events = policy.get("events", {})
    if event not in events:
        raise SelectionError(
            f"unknown event {event!r}; declared events: {sorted(events)}"
        )
    profile_sets = policy.get("profile_sets", {})
    known_ids = set(profiles)

    event_policy = events[event]
    if not isinstance(event_policy, dict):
        # An event key written with no body (e.g. `canary:` alone) parses
        # as None via yaml.safe_load, not {} -- .get() below would then
        # raise an unhandled AttributeError instead of the intended
        # SelectionError (CodeRabbit review, PR #19).
        raise SelectionError(
            f"events[{event!r}] must be a mapping, got {type(event_policy).__name__}"
        )
    required = sorted(set(_expand(event_policy.get("required", []), profile_sets, known_ids)))
    advisory = sorted(set(_expand(event_policy.get("advisory", []), profile_sets, known_ids)) - set(required))

    return {
        "event": event,
        "required": required,
        "advisory": advisory,
        "profiles": sorted(set(required) | set(advisory)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="one of ci/event-policy.yaml's events.* keys")
    parser.add_argument("--profiles-file", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument(
        "--profile",
        action="append",
        default=None,
        dest="profile_filter",
        help="restrict output to this profile id (repeatable)",
    )
    # PR4 item 2 (release.yml's own use): a release-contract baseline is
    # only ever published for a profile whose result is actually trusted
    # (ci/profiles.yaml's `contract: true`) -- release.yml needs exactly
    # that subset of whatever an event's own policy resolves, not every
    # advisory profile events.release's own `advisory: [core, advisory]`
    # entry (pre-existing since PR1, never consumed by a workflow before
    # this) would otherwise hand it.
    parser.add_argument(
        "--contract-only",
        action="store_true",
        help="restrict output to profiles ci/profiles.yaml marks contract: true",
    )
    args = parser.parse_args(argv)

    try:
        known = load_profiles(args.profiles_file)
        result = select(args.event, args.profiles_file, args.policy_file)
        if args.profile_filter:
            for pid in args.profile_filter:
                if pid not in known:
                    raise SelectionError(f"unknown profile id: {pid!r}")
            wanted = set(args.profile_filter)
            result["required"] = [p for p in result["required"] if p in wanted]
            result["advisory"] = [p for p in result["advisory"] if p in wanted]
            result["profiles"] = [p for p in result["profiles"] if p in wanted]
        if args.contract_only:
            contract_ids = {pid for pid, entry in known.items() if entry.get("contract") is True}
            result["required"] = [p for p in result["required"] if p in contract_ids]
            result["advisory"] = [p for p in result["advisory"] if p in contract_ids]
            result["profiles"] = [p for p in result["profiles"] if p in contract_ids]
    except SelectionError as exc:
        print(f"select_profiles: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
