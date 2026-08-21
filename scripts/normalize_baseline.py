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
    # `git_commit` changes on every push to main regardless of whether the
    # ABI did -- defeating the "commit only when the ABI actually changed"
    # goal this script exists for. Git history on abi/math.abicheck.json
    # already records which commit each refresh corresponds to. Safe to
    # delete outright: abicheck's own loader reads it via `d.get("git_commit")`
    # (verified against abicheck/serialization.py's `snapshot_from_dict`),
    # so an absent key loads as None, same as a legacy snapshot predating
    # the field.
    #
    # `version` does NOT belong in this set, unlike an earlier revision of
    # this script (Codex review, real regression: the first baseline
    # refresh with `version` stripped produced a committed
    # abi/math.abicheck.json that every subsequent PR's scan/compare failed
    # to even load -- "Failed to load JSON snapshot ...: 'version'" --
    # because `snapshot_from_dict` reads it as `d["version"]`, a required
    # key with no default, not `.get()` like `git_commit`/`git_tag`. See
    # VERSION_NORMALIZE_PATH below for how this script now keeps the same
    # "don't commit a diff for a value abicheck computes fresh every run"
    # goal without deleting a key the loader can't do without.
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
    # ci/real_scan.py's own dump_real_snapshot() invocation (cmake/make
    # profile baselines' embedded abicheck_snapshot): --build-info is the
    # filtered per-target compile database's own path under staged_dir,
    # and --sources is always this repo's own absolute checkout root
    # (REPO_ROOT). abicheck records both verbatim in the snapshot it
    # produces, and again per-extractor -- a baseline refreshed on one
    # checkout root (e.g. main's own CI runner) compared/verified against a
    # dump taken from a different one (a local refresh, or release
    # verification re-running on a fresh checkout) then differs only in
    # this host-specific path text, not any actual ABI content
    # (Codex review, PR #25: "the embedded snapshot still differs because
    # the filtered compile-database path is recorded verbatim ... so
    # apply_profile_baselines.py --verify-only can report byte-level drift
    # despite an unchanged ABI"). Verified against a real embedded
    # cmake-ninja/math snapshot: both fields are present exactly at these
    # paths.
    ("build_source", "manifest", "inputs", "build_info"),
    ("build_source", "manifest", "inputs", "sources"),
    ("build_source", "manifest", "extractors", ANY_INDEX, "inputs", ANY_INDEX),
    # Same checkout-root noise, one level deeper in the real scanner's own
    # source-level evidence (verified against the same real cmake-ninja/math
    # snapshot as the manifest.inputs entries above): every
    # reachable_source_surface entry's source_location.path is an absolute
    # path (e.g. "/home/runner/work/.../include/abicheck_lab/math.h"), and
    # source_graph's own node ids/labels and edge endpoints embed the
    # identical absolute path behind a "scheme://" prefix
    # (cu:///abs/path#cfg:..., source:///abs/path, header:///abs/path) --
    # _normalize_path_string()'s own scheme-preserving rewrite (see its
    # docstring) is what makes rewriting these safe without corrupting the
    # graph identifiers abicheck's own edges reference by (Codex review,
    # PR #25, round 4: "Normalize all checkout-root paths in embedded
    # dumps"). All five reachable_source_surface categories share the
    # identical {api_relevant, ..., source_location: {path, ...}} shape --
    # verified against abicheck's own model.py-derived output, not just the
    # two (declarations/types) this repo's own tiny fixture happens to
    # populate.
    ("build_source", "source_abi", "reachable_source_surface", "declarations", ANY_INDEX, "source_location", "path"),
    ("build_source", "source_abi", "reachable_source_surface", "inline_bodies", ANY_INDEX, "source_location", "path"),
    ("build_source", "source_abi", "reachable_source_surface", "macros", ANY_INDEX, "source_location", "path"),
    ("build_source", "source_abi", "reachable_source_surface", "templates", ANY_INDEX, "source_location", "path"),
    ("build_source", "source_abi", "reachable_source_surface", "types", ANY_INDEX, "source_location", "path"),
    # source_graph node kinds that never embed a filesystem path at all
    # (build_option://, binary_symbol://, decl://, type://, debug_type://
    # -- mangled names, qualified names, sha256 hashes) simply never match
    # the marker search below and pass through unchanged; only
    # compile_unit/source/header nodes (cu://, source://, header://) and
    # the edges connecting them actually need this.
    ("build_source", "source_graph", "nodes", ANY_INDEX, "id"),
    ("build_source", "source_graph", "nodes", ANY_INDEX, "label"),
    ("build_source", "source_graph", "edges", ANY_INDEX, "src"),
    ("build_source", "source_graph", "edges", ANY_INDEX, "dst"),
})

# NOT yet handled -- a real, still-open gap from the same review round:
# source_abi.mappings.public_header_to_target's KEYS (not values) are
# absolute paths too (e.g.
# {"/abs/path/include/abicheck_lab/math.h": "<target>"}). Every path
# above is rewritten by walking to a fixed VALUE position; a dict key is
# structurally different (normalize_paths() below only ever recurses into
# values, never rewrites the key itself) and would need real, separate
# handling -- rebuilding the dict with renamed keys, deciding what happens
# if two differently-cased-or-rooted absolute paths collide onto the same
# relative suffix, etc. Doing that safely is more than this pass's other,
# mechanical additions; deferred rather than rushed. Comparability is
# unaffected in practice today (CI/release runs land at the same
# conventional checkout path), so this is a real but currently low-impact
# gap, not silently dropped.

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

# `version` is a required field of abicheck's own AbiSnapshot loader
# (`snapshot_from_dict`: `version=d["version"]`, no default -- unlike
# `git_commit`/`git_tag`, which use `.get()`). baseline.yml passes
# `new-version: main-${{ github.sha }}`, so the raw dump's `version` value
# changes on every push regardless of whether the ABI did. This script
# still wants "no commit unless the ABI actually changed", but can't get
# there by deleting the key the way DELETE_PATHS does for everything else
# -- that produces a baseline abicheck itself can no longer load at all
# (verified against a real broken commit: every later PR's scan/compare
# failed immediately with "Failed to load JSON snapshot ...: 'version'").
# Rewritten to a fixed placeholder instead: present (satisfies the
# loader), constant (no diff noise across refreshes). Git history on
# abi/math.abicheck.json already records which commit each refresh
# reflects, same reasoning DELETE_PATHS uses for git_commit/created_at.
VERSION_NORMALIZE_PATH = ("version",)
VERSION_PLACEHOLDER = "main"


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
    # manifest.inputs.sources is always the repo root itself (--sources
    # REPO_ROOT, see ci/real_scan.py) -- checked BEFORE the marker/rfind
    # search below, not after: a GitHub Actions checkout doubles the repo
    # dir name in its path (.../work/<repo>/<repo>), so a bare root value
    # like "/home/runner/work/integration-lab/integration-lab" already
    # contains one full "/integration-lab/" occurrence (between the two
    # segments) even though nothing legitimately follows it -- the rfind
    # search below would then treat the second segment's own name as if it
    # were relative content and return the bogus "integration-lab" instead
    # of recognizing the whole value as the root itself. "." is the same
    # "this is the source root, nothing to strip further" placeholder
    # abicheck's own CLI already accepts for --sources. (Only meaningful
    # for a value that IS an absolute path outright -- never for one only
    # embedding a path behind a scheme prefix, e.g. source_graph's own node
    # ids below; those never equal a bare repo root.)
    if value.startswith("/") and value.endswith(f"/{repo_root_marker}"):
        return "."

    # GitHub Actions checkouts land at .../work/<repo>/<repo>/..., so the
    # marker can legitimately appear twice; take the last occurrence to
    # strip the full checkout prefix rather than stopping at the first.
    #
    # Not every PATH_REWRITE_PATHS value IS an absolute path outright --
    # abicheck's own source_graph node ids/labels and edge endpoints
    # (`cu:///abs/path#cfg:...`, `source:///abs/path`, `header:///abs/path`)
    # and reachable_source_surface's per-declaration source_location.path
    # embed the SAME checkout-root noise, just with real graph structure
    # (a "scheme://" prefix, a "#cfg:..." fragment) around it that must
    # survive -- dropping it would break the identifiers abicheck's own
    # edges reference by (Codex review, PR #25, round 4: "Normalize all
    # checkout-root paths in embedded dumps ... including
    # reachable_source_surface[...].source_location.path and multiple
    # source_graph node/edge identifiers"). rfind (not a startswith check)
    # finds the marker wherever it sits in the string, and whatever comes
    # before it -- a "scheme://" prefix, or nothing at all for a bare
    # absolute path -- is preserved untouched; only the checkout-root
    # portion itself is stripped. A value with no scheme prefix and no
    # marker match at all (most strings in this tree: mangled names, type
    # names, hashes -- see the PATH_REWRITE_PATHS entries' own comment for
    # why plain rfind is safe even for those) falls through unchanged.
    marker = f"/{repo_root_marker}/"
    idx = value.rfind(marker)
    if idx != -1:
        scheme_end = value.rfind("://", 0, idx)
        prefix = value[: scheme_end + 3] if scheme_end != -1 else ""
        return prefix + value[idx + len(marker):]

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


def normalize_version(report):
    # Only touches the document-root `version` key -- matching
    # VERSION_NORMALIZE_PATH's exact path, not any key named "version"
    # anywhere in the tree (same name-keyed-map hazard the rest of this
    # script is written to avoid; see the module docstring). If the key is
    # missing entirely (unexpected -- abicheck's own dump always sets it),
    # leave it missing rather than inventing one: that's a louder, more
    # honest failure downstream (the loader's own "required key" error)
    # than silently fabricating a value.
    if VERSION_NORMALIZE_PATH[0] in report:
        report = {**report, "version": VERSION_PLACEHOLDER}
    return report


def normalize(report, repo_root_marker):
    report = strip_volatile_paths(report)
    report = normalize_paths(report, repo_root_marker)
    report = normalize_timing_prose(report)
    report = normalize_version(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Raw abicheck report JSON produced by `mode: dump`")
    parser.add_argument("output", help="Path to write the normalized, commit-ready JSON")
    parser.add_argument(
        "--repo-root-marker",
        default="integration-lab",
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
