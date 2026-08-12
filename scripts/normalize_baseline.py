#!/usr/bin/env python3
"""Normalize an ABICheck report before it is committed as a baseline.

The raw output of `abicheck --mode dump` embeds volatile, run-specific
metadata: wall-clock timestamps, absolute runner paths, filesystem mtimes,
and per-run timing numbers. None of that reflects the public ABI, so
committing it verbatim turns every baseline refresh into a noisy diff even
when nothing observable changed.

This script strips or rewrites exactly that class of field:

  * volatile keys (created_at, source_mtime*, source_size, build_id, ...)
    are dropped wherever they occur, at any depth
  * absolute paths that contain the repo checkout directory are rewritten
    to repo-relative paths
  * absolute paths into the Bazel execroot/output tree are rewritten to
    the bazel-out-relative fragment
  * "<number>s" timing fragments inside free-text extractor "detail"
    strings are replaced with a constant placeholder so re-running the
    same build twice produces byte-identical prose

Everything that reflects the actual public ABI/API (symbols, types,
signatures, coverage status, source-relative locations, toolchain
identity, semantic content hash) is left untouched.
"""
import argparse
import json
import re
import sys

# Keys dropped wherever they appear in the report, at any nesting depth.
# These only ever carry run-provenance / timing information.
VOLATILE_KEYS = {
    "created_at",
    "source_mtime",
    "source_mtime_epoch",
    "source_size",
    "build_id",
}

TIMING_RE = re.compile(r"\d+(?:\.\d+)?s\b")


def strip_volatile_keys(node):
    if isinstance(node, dict):
        return {
            key: strip_volatile_keys(value)
            for key, value in node.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(node, list):
        return [strip_volatile_keys(item) for item in node]
    return node


def normalize_paths(node, repo_root_marker):
    """Rewrite absolute paths to relative ones.

    Any string containing the repo's directory name is truncated to the
    repo-relative suffix. Any string that still looks like an absolute
    path but reaches into a Bazel execroot/output tree is truncated to
    the `bazel-out/...` (or `external/...`) relative fragment.
    """
    if isinstance(node, dict):
        return {key: normalize_paths(value, repo_root_marker) for key, value in node.items()}
    if isinstance(node, list):
        return [normalize_paths(item, repo_root_marker) for item in node]
    if isinstance(node, str):
        return _normalize_path_string(node, repo_root_marker)
    return node


def _normalize_path_string(value, repo_root_marker):
    if not value.startswith("/"):
        return value

    # GitHub Actions checkouts land at .../work/<repo>/<repo>/..., so the
    # marker can legitimately appear twice; take the last occurrence to
    # strip the full checkout prefix rather than stopping at the first.
    marker = f"/{repo_root_marker}/"
    idx = value.rfind(marker)
    if idx != -1:
        return value[idx + len(marker):]

    for anchor in ("bazel-out/", "external/"):
        idx = value.find(anchor)
        if idx != -1:
            return value[idx:]

    return value


def normalize_timing_prose(node):
    if isinstance(node, dict):
        return {key: normalize_timing_prose(value) for key, value in node.items()}
    if isinstance(node, list):
        return [normalize_timing_prose(item) for item in node]
    if isinstance(node, str):
        return TIMING_RE.sub("Ns", node)
    return node


def normalize(report, repo_root_marker):
    report = strip_volatile_keys(report)
    report = normalize_paths(report, repo_root_marker)
    report = normalize_timing_prose(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Raw abicheck report JSON produced by `mode: dump`")
    parser.add_argument("output", help="Path to write the normalized, commit-ready JSON")
    parser.add_argument(
        "--repo-root-marker",
        default="abicheck-bazel-lab",
        help="Repo directory name to strip from absolute paths (default: %(default)s)",
    )
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as fh:
        report = json.load(fh)

    normalized = normalize(report, args.repo_root_marker)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(normalized, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    sys.exit(main())
