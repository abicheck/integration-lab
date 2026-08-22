"""Unit tests for scripts/normalize_baseline.py -- specifically
_normalize_path_string() and the PATH_REWRITE_PATHS entries added for
ci/real_scan.py's embedded abicheck_snapshot (cmake/make profile
baselines), which this repo had no dedicated test coverage for before
(Codex review, PR #25, rounds 3-4: manifest.inputs.*, extractors[].inputs[],
reachable_source_surface[...].source_location.path, and source_graph node/
edge identifiers all embed the checkout root the same way plain absolute
paths do, just with real structure -- a "scheme://" prefix, a "#cfg:..."
fragment -- that must survive the rewrite intact).
"""
from __future__ import annotations

from normalize_baseline import _normalize_path_string, normalize


def test_bare_repo_root_normalizes_to_placeholder():
    # manifest.inputs.sources is always the repo root itself (--sources
    # REPO_ROOT) -- nothing follows the marker to preserve.
    assert _normalize_path_string("/home/user/integration-lab", "integration-lab") == "."


def test_doubled_checkout_bare_root_normalizes_to_placeholder():
    # GitHub Actions checkouts double the repo dir name
    # (.../work/<repo>/<repo>) -- a bare root value in that shape already
    # contains one full "/integration-lab/" occurrence between the two
    # segments even though nothing legitimately follows it. A real bug,
    # found and fixed while adding this case: the rfind-based search
    # treated the second segment's own name as relative content instead of
    # recognizing the whole value as the root.
    assert _normalize_path_string(
        "/home/runner/work/integration-lab/integration-lab", "integration-lab"
    ) == "."


def test_plain_absolute_path_strips_to_relative():
    assert _normalize_path_string(
        "/home/user/integration-lab/include/abicheck_lab/math.h", "integration-lab"
    ) == "include/abicheck_lab/math.h"


def test_doubled_checkout_file_path_still_strips_correctly():
    assert _normalize_path_string(
        "/home/runner/work/integration-lab/integration-lab/abicheck-build-x/"
        "evidence/.real-scan-tmp-math/compile_commands.json",
        "integration-lab",
    ) == "abicheck-build-x/evidence/.real-scan-tmp-math/compile_commands.json"


def test_scheme_prefixed_path_preserves_scheme_and_fragment():
    # source_graph node ids embed an absolute path behind a "scheme://"
    # prefix and, for compile units, a "#cfg:..." fragment after it --
    # both must survive; only the checkout-root portion is noise.
    assert _normalize_path_string(
        "cu:///home/user/integration-lab/src/math.cc#cfg:f8b0fc56724e", "integration-lab"
    ) == "cu://src/math.cc#cfg:f8b0fc56724e"
    assert _normalize_path_string(
        "source:///home/user/integration-lab/src/math.cc", "integration-lab"
    ) == "source://src/math.cc"
    assert _normalize_path_string(
        "header:///home/user/integration-lab/include/abicheck_lab/math.h", "integration-lab"
    ) == "header://include/abicheck_lab/math.h"


def test_non_path_scheme_values_pass_through_unchanged():
    # Most source_graph node kinds never embed a filesystem path at all
    # (mangled names, qualified names, sha256 hashes) -- these must never
    # be altered by a marker search that simply doesn't match.
    for value in (
        "build_option://-std=c++17",
        "binary_symbol://_ZN12abicheck_lab11api_versionEv",
        "decl://_ZN12abicheck_lab10CalculatorC1Ev",
        "type://abicheck_lab::Calculator",
        "debug_type://sha256:8ec4118107a2b9391abdd184af9e114fdd87d50dc3307121460c9de7c2535241",
    ):
        assert _normalize_path_string(value, "integration-lab") == value


def test_bazel_out_anchor_still_handled():
    assert _normalize_path_string(
        "/home/user/.cache/bazel/execroot/_main/bazel-out/k8-fastbuild/bin/foo",
        "integration-lab",
    ) == "bazel-out/k8-fastbuild/bin/foo"


def test_normalize_rewrites_reachable_source_surface_and_source_graph():
    snapshot = {
        "build_source": {
            "source_abi": {
                "reachable_source_surface": {
                    "declarations": [
                        {"source_location": {"path": "/home/user/integration-lab/include/x.h", "line": 1}},
                    ],
                    "inline_bodies": [],
                    "macros": [],
                    "templates": [],
                    "types": [
                        {"source_location": {"path": "/home/user/integration-lab/include/x.h", "line": 2}},
                    ],
                },
                "mappings": {
                    # Deliberately NOT rewritten yet (dict keys, not values) --
                    # see PATH_REWRITE_PATHS's own "NOT yet handled" comment.
                    "public_header_to_target": {"/home/user/integration-lab/include/x.h": ""},
                },
            },
            "source_graph": {
                "nodes": [
                    {"id": "cu:///home/user/integration-lab/src/x.cc#cfg:abc", "label": "/home/user/integration-lab/src/x.cc", "kind": "compile_unit"},
                    {"id": "type://SomeType", "label": "SomeType", "kind": "record_type"},
                ],
                "edges": [
                    {"src": "cu:///home/user/integration-lab/src/x.cc#cfg:abc", "dst": "source:///home/user/integration-lab/src/x.cc", "edge": "COMPILE_UNIT_BUILDS_SOURCE"},
                ],
            },
        },
    }

    normalized = normalize(snapshot, "integration-lab")
    rss = normalized["build_source"]["source_abi"]["reachable_source_surface"]
    assert rss["declarations"][0]["source_location"]["path"] == "include/x.h"
    assert rss["types"][0]["source_location"]["path"] == "include/x.h"

    nodes = normalized["build_source"]["source_graph"]["nodes"]
    assert nodes[0]["id"] == "cu://src/x.cc#cfg:abc"
    assert nodes[0]["label"] == "src/x.cc"
    assert nodes[1]["id"] == "type://SomeType"  # unaffected, no path to strip

    edge = normalized["build_source"]["source_graph"]["edges"][0]
    assert edge["src"] == "cu://src/x.cc#cfg:abc"
    assert edge["dst"] == "source://src/x.cc"

    # The documented, still-open gap: dict keys are untouched.
    mappings = normalized["build_source"]["source_abi"]["mappings"]["public_header_to_target"]
    assert "/home/user/integration-lab/include/x.h" in mappings
