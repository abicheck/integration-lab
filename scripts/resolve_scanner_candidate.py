#!/usr/bin/env python3
"""Resolve a scanner-candidate identity for canary.yml (PR5, design doc
section 23.3 "Scanner release candidate" + section 9.4 "canary.yml").

canary.yml can be triggered three ways, each naming the abicheck commit
to certify differently:

  * `repository_dispatch` (type `scanner-candidate`): the abicheck repo
    itself (or its release automation) dispatches a certification request
    with a JSON `client_payload`:
        {"scanner_repository": "abicheck/abicheck",
         "scanner_sha": "<exact commit SHA>",
         "scanner_version": "<optional>",
         "baseline_generation": "<optional>"}
    Section 23.3 is explicit: validate this payload and reject a missing
    or non-SHA scanner identifier -- a canary run silently certifying the
    wrong commit (or "whatever main happens to be right now" because a
    typo'd field was ignored) is worse than refusing to run at all.

  * `workflow_dispatch`: a human names a `scanner-ref` (branch, tag, or
    SHA; defaults to `main`) to spot-check on demand. Unlike the
    dispatch payload this is allowed to be a symbolic ref -- it gets
    resolved to an exact SHA via `git ls-remote` so every downstream job
    still certifies one pinned commit, not a moving branch tip.

  * `schedule`: no operator input at all -- resolves `main` on
    `abicheck/abicheck`, same as scenarios-canary.yml's existing
    Bazel-only canary already does today.

Kept as a standalone, network-optional script (network only happens in
`resolve_ref_to_sha`, and only for the workflow_dispatch/schedule paths)
so `validate_dispatch_payload` -- the actual security-relevant validation
section 23.3 asks for -- is unit-testable with zero network access, same
posture as this repo's other ci/scripts/*.py.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

DEFAULT_SCANNER_REPOSITORY = "abicheck/abicheck"
DEFAULT_SCANNER_REF = "main"

# Full 40-character lowercase-hex git SHA-1. abicheck's own commits (and
# GitHub's `client_payload`/`github.sha` values) are always this shape;
# accepting anything shorter or non-hex would let a truncated/garbled
# identifier through and have every downstream job silently operate on
# "whatever `git rev-parse` feels like resolving that prefix to today".
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ResolutionError(ValueError):
    """Raised for a payload/input that fails validation -- always a hard
    failure (exit 1), never a warning, per section 23.3's "reject missing
    or non-SHA scanner identifiers" instruction."""


def _pip_spec(repository: str, sha: str) -> str:
    return f"abicheck @ git+https://github.com/{repository}.git@{sha}"


def validate_dispatch_payload(payload: dict) -> dict:
    """Validate a `repository_dispatch` `client_payload` per section 23.3.

    Pure function, no network/subprocess -- this is the actual security
    boundary (an external repository_dispatch caller controls every field
    here), so it stays independently unit-testable.
    """
    if not isinstance(payload, dict):
        raise ResolutionError(f"client_payload must be a JSON object, got {type(payload).__name__}")

    repository = payload.get("scanner_repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ResolutionError("client_payload.scanner_repository is required and must be a non-empty string")
    # Deliberately not restricted to DEFAULT_SCANNER_REPOSITORY: a fork
    # candidate build (e.g. a contributor's own abicheck fork) is a
    # legitimate certification target. What's non-negotiable is the SHA.

    sha = payload.get("scanner_sha")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise ResolutionError(
            "client_payload.scanner_sha is required and must be an exact 40-character "
            f"hex commit SHA (got {sha!r})"
        )

    version = payload.get("scanner_version")
    if version is not None and not isinstance(version, str):
        raise ResolutionError(f"client_payload.scanner_version must be a string when present (got {version!r})")

    baseline_generation = payload.get("baseline_generation")
    if baseline_generation is not None and not isinstance(baseline_generation, str):
        raise ResolutionError(
            f"client_payload.baseline_generation must be a string when present (got {baseline_generation!r})"
        )

    return {
        "source_event": "repository_dispatch",
        "scanner_repository": repository,
        "scanner_sha": sha,
        "scanner_version": version,
        "baseline_generation": baseline_generation,
        "pip_spec": _pip_spec(repository, sha),
    }


def resolve_ref_to_sha(
    repository: str,
    ref: str,
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> str:
    """Resolve a branch/tag/SHA `ref` on `repository` to an exact commit
    SHA via `git ls-remote` -- a ref already shaped like a full SHA is
    returned as-is without a network round-trip (both because it's
    unnecessary and because `git ls-remote <repo> <sha>` does not, in
    general, echo back a bare commit SHA the way it does a branch/tag).
    """
    if _SHA_RE.match(ref):
        return ref

    result = runner(
        ["git", "ls-remote", f"https://github.com/{repository}.git", ref],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ResolutionError(
            f"git ls-remote https://github.com/{repository}.git {ref!r} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        raise ResolutionError(f"git ls-remote found no ref {ref!r} on {repository!r}")

    # `git ls-remote` can list several matches for one ref name (e.g. a
    # branch and a tag sharing a name, or an annotated tag's own object
    # alongside its dereferenced `^{}` commit) -- take the first line's
    # SHA, same as `git ls-remote --exit-code` scripting convention.
    first_line = stdout.splitlines()[0]
    sha = first_line.split("\t", 1)[0].strip()
    if not _SHA_RE.match(sha):
        raise ResolutionError(f"git ls-remote returned an unexpected line for ref {ref!r}: {first_line!r}")
    return sha


def resolve_named_ref(
    repository: str,
    ref: str,
    source_event: str,
    runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> dict:
    sha = resolve_ref_to_sha(repository, ref, runner=runner)
    return {
        "source_event": source_event,
        "scanner_repository": repository,
        "scanner_sha": sha,
        "scanner_version": None,
        "baseline_generation": None,
        "pip_spec": _pip_spec(repository, sha),
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--event",
        required=True,
        choices=["repository_dispatch", "workflow_dispatch", "schedule"],
        help="which canary.yml trigger fired",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        help="path to a JSON file holding github.event.client_payload (required for --event repository_dispatch)",
    )
    parser.add_argument(
        "--scanner-repository",
        default=DEFAULT_SCANNER_REPOSITORY,
        help=f"owner/repo to resolve a ref on (workflow_dispatch/schedule only; default {DEFAULT_SCANNER_REPOSITORY})",
    )
    parser.add_argument(
        "--scanner-ref",
        default=DEFAULT_SCANNER_REF,
        help=f"branch/tag/SHA to resolve (workflow_dispatch/schedule only; default {DEFAULT_SCANNER_REF})",
    )
    args = parser.parse_args(argv)

    try:
        if args.event == "repository_dispatch":
            if not args.payload_file:
                raise ResolutionError("--payload-file is required for --event repository_dispatch")
            with args.payload_file.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            result = validate_dispatch_payload(payload)
        else:
            result = resolve_named_ref(args.scanner_repository, args.scanner_ref, args.event)
    except (ResolutionError, json.JSONDecodeError) as exc:
        print(f"resolve_scanner_candidate: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
