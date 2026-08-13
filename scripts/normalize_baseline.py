#!/usr/bin/env python3
"""Normalize an ABICheck report before it is committed as a baseline.

The raw output of `abicheck --mode dump` embeds volatile, run-specific
metadata: wall-clock timestamps, absolute runner paths, filesystem mtimes,
and per-run timing numbers. None of that reflects the public ABI, so
committing it verbatim turns every baseline refresh into a noisy diff even
when nothing observable changed.

## Design: an explicit allowlist of exact paths, not a key-name pattern

Earlier revisions of this script tried to strip "any key that looks
volatile" -- by name, or by name within a `build_source`/`build_source_pack`
subtree assumed to be pure provenance. Both turned out to be unsafe, twice
over (Codex review, in order of discovery):

  1. A key-name pattern applied to the *whole document* can match a real
     ABI entity's own name (a constant or function legitimately called
     `timeout_s` or `elapsed`), silently deleting it.
  2. Scoping that same pattern to "inside build_source/build_source_pack"
     doesn't fix it: those subtrees aren't pure provenance either --
     `build_source.build_evidence.compile_units[].defines` (preprocessor
     defines, ABI-relevant) and `build_source.source_abi`/`source_graph`
     (the actual source-linkage evidence: `mappings`, `source_edges`,
     `nodes`, `edges`, `indexes.by_target`/`by_file`/`by_binary_symbol`/
     `by_source_decl`, ...) are real content, keyed by real identifiers
     (file paths, symbol names, target labels) that could just as easily
     collide with a volatile-looking name.

The pattern in both cases is the same: **a name-keyed map, where the *key*
is itself arbitrary content, can't safely be filtered by key name** --
anywhere. The only names safe to match are ones that are *fixed schema
fields* of a dataclass-shaped object (always spelled exactly that way by
abicheck itself: `content_hash`, `created_at`, `elapsed_s`, `detail`, ...),
never the key of a name-keyed content map.

So instead of trying to characterize "safe" subtrees, this script deletes
only the exact, evidence-verified paths in `DELETE_PATHS` below -- each one
checked against the real committed `abi/math.abicheck.json` before being
added. A future abicheck version adding a new volatile field somewhere
else in the tree will show up as a small amount of committed-baseline
noise (annoying, visible, safe) rather than a silently deleted ABI entity
(invisible, unsafe). That asymmetry is deliberate.

Path rewriting (absolute -> repo/bazel-out-relative) uses the same
allowlist-of-exact-paths reasoning as DELETE_PATHS, not a name-name match
applied anywhere in the tree. An earlier revision matched `source_location`/
`source_header`/`source_path` by key name alone, regardless of where that
key appeared -- safe today (verified against the committed baseline: those
names only occur as the fixed `Function`/`Variable`/`RecordType`/`EnumType`
dataclass fields abicheck's own `model.py` defines them as), but a
name-keyed map elsewhere in the schema (e.g. `constants`, currently empty
in this baseline) could in principle have an entry whose own key happens to
be `source_path`, and its value -- real ABI content, not provenance --
would get silently rewritten (Codex review). `PATH_REWRITE_PATHS` below is
the same exact-path allowlist as `DELETE_PATHS`, each entry verified
against the real committed baseline (`functions[]`/`variables[]`/`types[]`/
`enums[].source_location`/`.source_header`, and the top-level
`source_path`), so a schema field at an unverified location is left
untouched (annoying, visible, safe) rather than silently mis-rewritten if
its value happens to collide. The only other absolute paths present
(`compile_units[].argv[0]`, `link_units[].linker_argv[0]`, e.g.
`/usr/bin/gcc`) are stable toolchain paths, not run-to-run noise, and are
left untouched.

Everything that reflects the actual public ABI/API (symbols, types,
signatures, coverage status, source-relative locations, toolchain
identity) is left untouched.
"""
import argparse
import json
import re
import sys

# Sentinel marking "any index of a list" in a DELETE_PATHS entry.
ANY_INDEX = object()

# Exact paths to delete, each verified against the real committed
# abi/math.abicheck.json before being added here -- see the module
# docstring for why this is a strict allowlist rather than a name pattern.
DELETE_PATHS = frozenset({
    # Wall-clock timestamp at the document root.
    ("created_at",),
    # The built .so's own filesystem metadata: a fresh build gets a new
    # mtime every run regardless of whether its content (and therefore the
    # ABI) changed at all -- lost from the allowlist in the exact-path
    # redesign, restored here (Codex review).
    ("source_mtime",),
    ("source_mtime_epoch",),
    ("source_size",),
    ("build_id",),
    # baseline.yml passes `new-version: main-${{ github.sha }}`, so both of
    # these change on every push to main regardless of whether the ABI
    # did -- defeating the "commit only when the ABI actually changed"
    # goal this script exists for. Git history on abi/math.abicheck.json
    # already records which commit each refresh corresponds to.
    ("version",),
    ("git_commit",),
    # Content digest computed over the raw (pre-normalization) payload --
    # still varies run-to-run even after everything else here is
    # normalized. We can't recompute abicheck's own hash algorithm, so the
    # honest fix is to drop the stale digest rather than commit one that
    # claims to be stable and isn't.
    ("build_source_pack", "content_hash"),
    ("build_source", "manifest", "created_at"),
    ("build_source", "manifest", "artifacts"),
    ("build_source", "manifest", "extractors", ANY_INDEX, "artifacts"),
    ("build_source", "manifest", "coverage", ANY_INDEX, "elapsed_s"),
    ("build_source", "source_abi", "coverage", "cache_lookup_s"),
    ("build_source", "source_abi", "coverage", "extract_s"),
    ("build_source", "source_abi", "coverage", "link_s"),
    ("build_source", "source_abi", "coverage", "elapsed_s"),
    ("build_source", "source_graph", "graph_id"),
})

# Exact paths known to hold an absolute filesystem path emitted by
# `abicheck --mode dump` -- verified against abicheck's own model.py:
# source_location/source_header are fixed dataclass fields of Function,
# Variable, RecordType (-> types), and EnumType; source_path is a fixed
# top-level AbiSnapshot field. Matched by exact path, not by key name alone
# -- see the module docstring for why a name-keyed map elsewhere in the
# schema (e.g. constants) makes name-only matching unsafe here too.
PATH_REWRITE_PATHS = frozenset({
    ("functions", ANY_INDEX, "source_location"),
    ("functions", ANY_INDEX, "source_header"),
    ("variables", ANY_INDEX, "source_location"),
    ("variables", ANY_INDEX, "source_header"),
    ("types", ANY_INDEX, "source_location"),
    ("types", ANY_INDEX, "source_header"),
    ("enums", ANY_INDEX, "source_location"),
    ("enums", ANY_INDEX, "source_header"),
    ("source_path",),
})

# Exact paths whose "detail" free-text field is provenance prose that
# embeds a per-run timing number -- verified against the real committed
# baseline (both locations that actually contain a "N.NNs" fragment).
# Matched by exact path, not by the key name "detail" anywhere in the
# tree: a genuine ABI-relevant public constant literally named "detail"
# with a value like "30s" (a chrono literal, say) would otherwise get
# silently corrupted (Codex review) -- same collision class DELETE_PATHS
# and PATH_REWRITE_PATHS were already redesigned around.
TIMING_REWRITE_PATHS = frozenset({
    ("build_source", "manifest", "extractors", ANY_INDEX, "detail"),
    ("build_source", "manifest", "coverage", ANY_INDEX, "detail"),
})

TIMING_RE = re.compile(r"\d+(?:\.\d+)?s\b")


def _matches(path, pattern):
    if len(path) != len(pattern):
        return False
    return all(p is ANY_INDEX or p == a for p, a in zip(pattern, path, strict=True))


def _in_pathset(path, pathset):
    return any(_matches(path, pattern) for pattern in pathset)


def strip_volatile_paths(node, path=()):
    if isinstance(node, dict):
        return {
            key: strip_volatile_paths(value, (*path, key))
            for key, value in node.items()
            if not _in_pathset((*path, key), DELETE_PATHS)
        }
    if isinstance(node, list):
        return [strip_volatile_paths(item, (*path, ANY_INDEX)) for item in node]
    return node


def normalize_paths(node, repo_root_marker, path=()):
    """Rewrite absolute paths to relative ones.

    Any string containing the repo's directory name is truncated to the
    repo-relative suffix. Any string that still looks like an absolute
    path but reaches into a Bazel execroot/output tree is truncated to
    the `bazel-out/...` (or `external/...`) relative fragment.

    Only applied at the exact schema paths in `PATH_REWRITE_PATHS` -- not
    every string whose immediate key happens to match one of those names,
    which would also rewrite an unrelated name-keyed map entry that
    happens to be named e.g. `source_path` (Codex review).
    """
    if isinstance(node, dict):
        return {k: normalize_paths(v, repo_root_marker, (*path, k)) for k, v in node.items()}
    if isinstance(node, list):
        return [normalize_paths(item, repo_root_marker, (*path, ANY_INDEX)) for item in node]
    if isinstance(node, str) and _in_pathset(path, PATH_REWRITE_PATHS):
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


def normalize_timing_prose(node, path=()):
    if isinstance(node, dict):
        return {k: normalize_timing_prose(v, (*path, k)) for k, v in node.items()}
    if isinstance(node, list):
        return [normalize_timing_prose(item, (*path, ANY_INDEX)) for item in node]
    if isinstance(node, str) and _in_pathset(path, TIMING_REWRITE_PATHS):
        return TIMING_RE.sub("Ns", node)
    return node


def normalize(report, repo_root_marker):
    report = strip_volatile_paths(report)
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
