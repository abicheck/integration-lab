#!/usr/bin/env python3
"""Normalize an ABICheck report before it is committed as a baseline.

The raw output of `abicheck --mode dump` embeds volatile, run-specific
metadata: wall-clock timestamps, absolute runner paths, filesystem mtimes,
and per-run timing numbers. None of that reflects the public ABI, so
committing it verbatim turns every baseline refresh into a noisy diff even
when nothing observable changed.

This script strips or rewrites exactly that class of field:

  * a handful of known top-level scalar fields (created_at, source_mtime*,
    source_size, build_id) are dropped
  * within the `build_source_pack`/`build_source` provenance subtrees only
    (never elsewhere in the document -- see below), volatile keys are
    dropped wherever they occur inside those subtrees: any `*_s`/elapsed/
    duration timing field, cache_hit_rate/ratio, and the hash digests
    computed over that volatile payload (content_hash, graph_id, artifacts)
  * absolute paths that contain the repo checkout directory are rewritten
    to repo-relative paths, everywhere in the document
  * absolute paths into the Bazel execroot/output tree are rewritten to
    the bazel-out-relative fragment, everywhere in the document
  * "<number>s" timing fragments inside the free-text extractor "detail"
    field specifically (not arbitrary strings, so a genuine API constant
    like a `30s` chrono literal is never touched) are replaced with a
    constant placeholder so re-running the same build twice produces
    byte-identical prose

Why the key-name stripping is scoped to two named subtrees rather than
applied to the whole document: the report also carries actual ABI content
under name-keyed maps (`constants`, `functions`, `variables`, `types`,
`enums`, `typedefs`, ...). A real public constant or entity can legitimately
be named `timeout_s` or `elapsed`; stripping by key name anywhere in the
tree would silently delete that entity from the committed baseline instead
of just some provenance metadata, hiding a real removal/change from every
future comparison (Codex review). Restricting the stripping to
`build_source_pack`/`build_source` -- the only places these volatile
fields have actually been observed -- keeps the blast radius to provenance
metadata only.

Path rewriting and the "detail"-scoped timing substitution stay global:
they act on string *values*, not dict *keys*, so they can't collide with
and delete a same-named ABI entity the way key stripping could.

Everything that reflects the actual public ABI/API (symbols, types,
signatures, coverage status, source-relative locations, toolchain
identity) is left untouched. The digest fields are the one exception:
abicheck computes them over the raw payload *before* this script runs, so
they still vary run-to-run even after normalization; we drop the stale
digest rather than commit one that claims to be stable and isn't.
"""
import argparse
import json
import re
import sys

# Top-level scalar fields dropped unconditionally. These are only ever
# emitted at the document root by `abicheck --mode dump`, never as a
# same-named entity nested under functions/types/constants/etc., so
# there's no ambiguity in stripping them by name here.
TOP_LEVEL_VOLATILE_KEYS = {
    "created_at",
    "source_mtime",
    "source_mtime_epoch",
    "source_size",
    "build_id",
    # baseline.yml passes `new-version: main-${{ github.sha }}`, so both of
    # these change on *every* push to main regardless of whether the ABI
    # did -- defeating the "commit only when the ABI actually changed"
    # goal this whole script exists for (Codex review: a diff-suppression
    # check that never suppresses isn't one). git history on this file
    # already records which commit each baseline refresh corresponds to,
    # so there's no information lost by not duplicating that SHA here too.
    "version",
    "git_commit",
}

# Named subtrees that hold nothing but build/collection provenance -- never
# ABI content -- so it's safe to strip volatile keys recursively *within*
# them. Everything outside these subtrees (functions, variables, types,
# constants, enums, typedefs, elf/dwarf symbol tables, ...) is left alone
# by the key-based stripping below.
_PROVENANCE_CONTAINER_KEYS = ("build_source_pack", "build_source")

# Keys dropped wherever they appear *inside a provenance container* above.
# These only ever carry run-provenance / timing information there.
_PROVENANCE_VOLATILE_KEYS = {
    "created_at",
    "cache_hit_rate",
    "cache_hit_ratio",
    # Content digests computed by abicheck over the raw (pre-normalization)
    # payload, which still embeds the volatile fields above -- so the
    # digest itself varies run-to-run even when the ABI doesn't. We can't
    # recompute abicheck's own hash algorithm here, so the honest fix is
    # to drop the stale digest rather than commit one that lies about
    # being stable.
    "content_hash",
    "graph_id",
    "artifacts",
}

# Catches other per-run numeric timing fields without having to enumerate
# every extractor's field name (e.g. a future `parse_s`, `query_s`,
# `cache_lookup_s`, ...): anything named like a "<phase>_s" seconds
# counter, or an explicit elapsed/duration field. Plain `str.endswith`
# rather than a `$`-anchored regex fed to `re.match` -- `re.match` only
# anchors at the *start* of the string, so a `_s$` alternative there would
# only ever match the literal two-character key "_s". Applied only inside
# a provenance container, same as _PROVENANCE_VOLATILE_KEYS above.
_PROVENANCE_VOLATILE_KEY_EXACT = {"elapsed", "elapsed_s", "duration", "duration_s"}

# Only applied to the extractor "detail" free-text field -- not to
# arbitrary strings, so a genuine ABI-relevant string constant (e.g. a
# `30s` chrono literal in a default argument) is never rewritten.
TIMING_RE = re.compile(r"\d+(?:\.\d+)?s\b")


def _is_provenance_volatile_key(key):
    return (
        key in _PROVENANCE_VOLATILE_KEYS
        or key in _PROVENANCE_VOLATILE_KEY_EXACT
        or key.endswith("_s")
    )


def _strip_provenance_volatile_keys(node):
    """Recursively drop volatile keys -- used only inside a provenance
    container, where every key is metadata and none is an ABI entity name.
    """
    if isinstance(node, dict):
        return {
            key: _strip_provenance_volatile_keys(value)
            for key, value in node.items()
            if not _is_provenance_volatile_key(key)
        }
    if isinstance(node, list):
        return [_strip_provenance_volatile_keys(item) for item in node]
    return node


def strip_volatile_keys(report):
    report = dict(report)
    for key in TOP_LEVEL_VOLATILE_KEYS:
        report.pop(key, None)
    for container_key in _PROVENANCE_CONTAINER_KEYS:
        if container_key in report:
            report[container_key] = _strip_provenance_volatile_keys(report[container_key])
    return report


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


def normalize_timing_prose(node, in_detail_field=False):
    if isinstance(node, dict):
        return {
            key: normalize_timing_prose(value, in_detail_field=(key == "detail"))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [normalize_timing_prose(item, in_detail_field=in_detail_field) for item in node]
    if isinstance(node, str) and in_detail_field:
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
