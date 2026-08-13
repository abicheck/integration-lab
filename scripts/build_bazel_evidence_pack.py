#!/usr/bin/env python3
"""Build a BuildSourcePack combining Bazel cquery target resolution with
aquery compile/link-unit evidence, for `abicheck ... --build-info`.

The problem this closes: abicheck v0.5.0's zero-config `--sources` path only
ever auto-runs `bazel aquery` (`buildsource/build_query.py`'s
`inferred_query_command`) -- never `bazel cquery` -- so `BuildEvidence.targets`
stays empty no matter what the Bazel workspace actually contains. Verified
against abicheck's own source (not guessed): `BazelAdapter._collect_cquery`
is the *only* method that ever appends to `ev.targets`; `_collect_aquery`
only ever populates `ev.compile_units`/`ev.link_units`. This is why this
lab's real scan reports always showed "N compile units, 0 targets" even
though the workspace resolves fine.

And the one officially exposed way to supply pre-captured evidence
(`--build-info <bazel-jsonproto-file>`) can't fix that either:
`buildsource/inline.py`'s `_maybe_collect_bazel_build_info` sniffs the file
and dispatches to *either* cquery *or* aquery, never both -- so a single
pre-captured file gives targets OR compile units, not both at once. The CLI
`collect --from bazel-cquery=... --from bazel-aquery=...` command that
*would* combine both was removed in the ADR-043 CLI reset (still present as
an internal helper, `_run_adapters`, explicitly "re-exported for API
stability / tests" in `cli_buildsource.py`, but no longer wired to any
command).

The fix: construct one `BazelAdapter` directly with *both* pre-captured
cquery and aquery jsonproto files (exactly what `collect --from` used to
wire up), producing a single `BuildEvidence` with `targets` (from cquery)
*and* `compile_units`/`link_units` (from aquery) populated together, and
write it out as a `BuildSourcePack` -- abicheck's own stable, versioned
on-disk pack format (`buildsource/pack.py`) -- so `--build-info <this-dir>`
loads it as a first-class pack (`is_pack_dir` validates the version marker
`BuildSourcePack.write()` sets automatically; no hand-rolled JSON schema
here).

Every name imported here (`BazelAdapter`, `BuildSourcePack`) is abicheck's
own internal API, verified against the exact pinned commit this repo's
abi-scan.yml uses for the `scan` job -- see the pip-install step this
script is a companion to.

Fails closed: any problem (bazel not on PATH, empty/degenerate query
output, an abicheck API surprise) prints a diagnostic to stderr and exits 1
*without* writing a pack directory, so the caller's own fallback (skip
--build-info, let abicheck's normal auto-inference run instead -- worse
evidence, but the pre-existing status quo, never a half-written pack
mistaken for a good one) kicks in instead.
"""
import argparse
import json
import sys
from pathlib import Path


def build_pack(cquery_path, aquery_path, workspace, output_dir, root_targets=()):
    """Return ``(None, summary_dict)`` on success, or ``(error_str, None)``.

    *root_targets* are the Bazel labels the caller actually asked to scan
    (e.g. ``//:math``) -- ``cquery deps(<label>)`` resolves *every*
    transitive dependency into ``ev.targets`` too, so a bare target count
    conflates "the library under test" with its whole dependency closure
    (e.g. `bazel_targets=32` for a workspace scanning one two-file library
    -- a real report from this pipeline). *summary_dict* separates the two
    so a caller can render "1 requested, 1 resolved, 31 transitive" instead
    of a single, misleading number.
    """
    try:
        from abicheck.buildsource.adapters.bazel import BazelAdapter
        from abicheck.buildsource.pack import BuildSourcePack
    except ImportError as exc:
        return f"abicheck not importable: {exc}", None

    if not cquery_path.is_file() or cquery_path.stat().st_size == 0:
        return f"{cquery_path} missing or empty (bazel cquery likely failed)", None
    if not aquery_path.is_file() or aquery_path.stat().st_size == 0:
        return f"{aquery_path} missing or empty (bazel aquery likely failed)", None

    adapter = BazelAdapter(
        workspace=workspace,
        cquery=cquery_path,
        aquery=aquery_path,
        # Pre-captured only: this must never spawn its own `bazel` process --
        # the cquery/aquery calls already happened as separate workflow
        # steps, verified-command-and-all (see the docstring above).
        allow_query=False,
    )
    ev = adapter.collect()

    if not ev.targets:
        return "cquery produced zero targets -- not writing a pack (would regress vs. auto-inference)", None
    if not ev.compile_units:
        return "aquery produced zero compile units -- not writing a pack (would regress vs. auto-inference)", None

    pack = BuildSourcePack.empty(output_dir)
    pack.build_evidence = ev
    pack.write()

    # `Target.id` is `"target://<label>"` (BazelAdapter's own convention --
    # see abicheck/buildsource/build_evidence.py:Target); a requested root
    # label like `//:math` therefore resolves as `target://` + that label.
    requested_ids = {f"target://{label}" for label in root_targets}
    resolved_root_ids = {t.id for t in ev.targets if t.id in requested_ids}
    summary = {
        "root_targets_requested": sorted(root_targets),
        "root_targets_resolved": sorted(t.split("target://", 1)[-1] for t in resolved_root_ids),
        "resolved_target_count": len(ev.targets),
        "transitive_target_count": len(ev.targets) - len(resolved_root_ids),
        "compile_unit_count": len(ev.compile_units),
        "link_unit_count": len(ev.link_units),
    }
    if root_targets and len(resolved_root_ids) < len(requested_ids):
        # CodeRabbit: the two ternary branches computed the same thing --
        # when resolved_root_ids is empty, every label trivially satisfies
        # "not in {}", so the comprehension alone already covers both cases.
        summary["root_targets_unresolved"] = sorted(
            label for label in root_targets if f"target://{label}" not in resolved_root_ids
        )

    print(
        f"build_bazel_evidence_pack: wrote {len(ev.targets)} target(s) "
        f"({len(resolved_root_ids)} requested root, "
        f"{summary['transitive_target_count']} transitive), "
        f"{len(ev.compile_units)} compile unit(s), {len(ev.link_units)} "
        f"link unit(s) to {output_dir}"
    )
    return None, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cquery", required=True, type=Path,
                         help="Path to `bazel cquery --output=jsonproto` output")
    parser.add_argument("--aquery", required=True, type=Path,
                         help="Path to `bazel aquery --output=jsonproto --include_param_files` output")
    parser.add_argument("--workspace", required=True, type=Path,
                         help="Bazel workspace root (anchors relative source/output paths)")
    parser.add_argument("--output", required=True, type=Path,
                         help="Directory to write the BuildSourcePack to")
    parser.add_argument(
        "--root-target", action="append", default=[], dest="root_targets",
        help="Bazel label actually requested for scanning (e.g. //:math); "
             "repeatable. Used only to separate 'requested root target(s)' "
             "from the transitive closure cquery also resolves -- never "
             "changes which targets get collected.",
    )
    parser.add_argument(
        "--summary-output", type=Path, default=None,
        help="Optional path to write the root/transitive target-count summary as JSON",
    )
    args = parser.parse_args()

    error, summary = build_pack(
        args.cquery, args.aquery, args.workspace, args.output, root_targets=args.root_targets
    )
    if error:
        print(f"build_bazel_evidence_pack: {error}", file=sys.stderr)
        return 1
    if args.summary_output is not None:
        args.summary_output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
