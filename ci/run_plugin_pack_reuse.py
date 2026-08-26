#!/usr/bin/env python3
"""Stage 3 of the plugin-pack scenario: reuse a facts pack in a CLEAN job.

The `l4_clang_plugin` job in `abi-scan.yml` builds the Clang plugin, runs it
as a declared Bazel action, verifies the pack is non-empty and compares with
it -- all in ONE job that also has the full source tree and a working build.
That proves the pack can be *produced*, and nothing about whether it can be
*consumed* anywhere else. A pack that silently only works next to the tree it
was collected from is not a portable evidence artifact; it is a cache.

This script is the consumer side, and it runs where that distinction is
observable: a separate job that downloads the binary, the public headers, the
baseline and the pack, and has no source checkout and no build. Everything it
concludes about source depth has to have come out of the pack.

Nine assertions, in the order the review named them:

1.  ``same-findings-as-replay``  -- the normalized finding multiset matches
    the portable-replay producer's, so reuse is not a different answer.
2.  ``same-effective-depth``     -- both sides prove ``source``.
3.  ``same-target-accounting``   -- both sides account for the same targets.
    Fails closed when NEITHER side names a target: two silent reports are
    not agreement (the same trap ``render_conformance_report.py`` documents
    for verdicts).
4.  ``pack-removed``             -- with the pack gone, the compare must NOT
    still hand back a complete source-depth report. This is the assertion
    that rules out a hidden replay fallback: in a job with no sources, a
    still-green L4 result means the depth came from somewhere other than the
    evidence we shipped.
5.  ``stale-source-digest``      -- a header edited after collection must be
    rejected. The pack has to be bound to the exact content it was collected
    from, or reuse silently mixes new headers with old facts.
6.  ``missing-tu``               -- one deleted ``source_facts/*.jsonl``.
7.  ``wrong-llvm-major``         -- the pack's recorded plugin/LLVM major
    changed. The plugin is ABI-locked to its loading Clang's LLVM major, so a
    pack from a different one is not interchangeable.
8.  ``empty-public-roots``       -- a pack declaring no public headers.
9.  ``corrupted-pack``           -- invalid JSONL, which must surface as an
    operational error rather than an empty-but-clean finding set.

"Rejected" deliberately admits four channels -- an operational exit status, a
non-empty ``operational_errors``, a failed source-depth contract, or no report
at all. Pinning each case to one exact channel would make this scenario fail
on an upstream change that merely moved *where* a real rejection is reported,
which is not the property being tested. Which channel actually fired is
recorded per case in the receipt, so a change in that shape is still visible.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from render_conformance_report import (  # noqa: E402
    _extract_findings,
    _multiset,
    _report_has_values,
)
from validate_source_depth import validate as validate_source_depth  # noqa: E402

VERDICT_EXIT_CODES = frozenset({0, 1, 2, 4})

#: Paths whose presence means this job is not the clean job it claims to be.
#: Anything here would let an implicit replay/AST path re-derive source facts
#: locally, which is exactly the fallback assertion 4 exists to exclude.
SOURCE_TREE_MARKERS: tuple[str, ...] = (
    "src",
    "include",
    "bazel-bin",
    "bazel-out",
    "BUILD.bazel",
    "MODULE.bazel",
    "CMakeLists.txt",
)

#: Manifest keys that could carry the COLLECTING PLUGIN'S LLVM identity. The
#: real pack manifest is produced upstream, so this is a search rather than a
#: fixed key -- and finding none is reported as a failure, not skipped: an
#: unversioned pack is precisely the interchangeability hazard assertion 7 is
#: about.
#:
#: Codex review: a bare "version" hint was wrong and made the assertion pass
#: for the wrong reason. The pack manifest scripts/merge_abicheck_facts.py
#: emits carries `abicheck_inputs_version` (the pack FORMAT version) and an
#: empty `version`; the substring search picked up the former, so the case
#: bumped a schema version and a rejection proved only that a malformed
#: format version is rejected -- never that a pack from a different LLVM
#: major is. The hints below name the producer's identity and nothing else,
#: and PACK_SCHEMA_KEYS is excluded explicitly so a future
#: `<something>_version` key cannot quietly re-open the same hole.
VERSION_KEY_HINTS: tuple[str, ...] = ("llvm", "clang", "compiler", "plugin")

#: Keys describing the pack's own FORMAT, never its producer. Mutating one of
#: these tests schema validation, which is a different property.
PACK_SCHEMA_KEYS: frozenset[str] = frozenset(
    {"abicheck_inputs_version", "version", "kind"}
)

#: Manifest keys that could carry the declared public header roots.
PUBLIC_ROOT_KEY_HINTS: tuple[str, ...] = ("headers", "public_headers", "public_roots")


class ScenarioError(RuntimeError):
    """A failure in the harness itself, distinct from a failed assertion."""


@dataclass
class Outcome:
    """One `abicheck compare` invocation and everything we judge it by."""

    exit_code: int
    report: dict[str, Any] | None
    depth_errors: list[str] = field(default_factory=list)

    def rejection_channel(self) -> str | None:
        """How this run failed closed, or None if it produced a clean L4 result."""
        if self.exit_code not in VERDICT_EXIT_CODES:
            return f"operational-exit:{self.exit_code}"
        if self.report is None:
            return "no-report"
        if self.report.get("operational_errors"):
            return "operational-errors"
        if self.depth_errors:
            return "depth-contract"
        return None


def run_compare(
    *,
    baseline: Path,
    binary: Path,
    header: Path,
    pack: Path | None,
    out: Path,
    expected_depth: str = "source",
) -> Outcome:
    """Compare against *baseline* using only shipped evidence.

    No ``--sources``: the whole point is that this job has no source tree to
    offer. ``--ast-frontend clang`` matches the producer side, so a
    divergence here is a producer divergence and not a frontend one.
    """
    out.unlink(missing_ok=True)
    argv = [
        "abicheck", "compare", str(baseline), str(binary),
        "--header", f"new={header}",
        "--ast-frontend", "clang",
        "--depth", "source",
        "--require-complete-analysis",
        "--format", "json",
        "-o", str(out),
        "--policy", "strict_abi",
    ]
    if pack is not None:
        argv += ["--build-info", f"new={pack}"]
    result = subprocess.run(argv)
    report: dict[str, Any] | None = None
    if out.is_file() and out.stat().st_size:
        try:
            loaded = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            report = loaded
    depth_errors = (
        validate_source_depth(report, expected=expected_depth, require_complete=True)
        if report is not None
        else ["no report was produced"]
    )
    return Outcome(exit_code=result.returncode, report=report, depth_errors=depth_errors)


def assert_clean_workspace(root: Path) -> list[str]:
    """Fail unless *root* really is source-free and build-free."""
    found = [name for name in SOURCE_TREE_MARKERS if (root / name).exists()]
    if found:
        return [
            "workspace is not clean: found "
            + ", ".join(sorted(found))
            + " -- this job must consume the shipped pack, not rebuild the evidence"
        ]
    return []


def normalized_findings(report: dict[str, Any] | None, other: dict[str, Any] | None):
    """The ``(kind, symbol[, values])`` multiset shared with the replay side.

    Values are folded in only when BOTH shapes carry them; a scan-mode report
    never does, so including them would compare real values against empty
    strings and never match (the reasoning ``render_conformance_report.py``
    already worked out for this exact pairing).
    """
    include_values = _report_has_values(report) and _report_has_values(other)
    return _multiset(_extract_findings(report), include_values=include_values)


def effective_depth(report: dict[str, Any] | None) -> Any:
    if not isinstance(report, dict):
        return None
    for key in ("analysis_assurance", "dump_provenance"):
        block = report.get(key)
        if isinstance(block, dict) and block.get("effective_depth") is not None:
            return block["effective_depth"]
    level = report.get("level")
    if isinstance(level, dict):
        return level.get("depth")
    return None


def target_accounting(report: dict[str, Any] | None) -> set[str]:
    """The set of target/library identities a report accounts for."""
    if not isinstance(report, dict):
        return set()
    names: set[str] = set()
    for key in ("libraries", "targets"):
        block = report.get(key)
        if isinstance(block, dict):
            names |= {str(k) for k in block}
        elif isinstance(block, list):
            for entry in block:
                if isinstance(entry, dict):
                    ident = entry.get("name") or entry.get("id") or entry.get("library")
                    if ident:
                        names.add(str(ident))
                elif isinstance(entry, str):
                    names.add(entry)
    for key in ("library", "target"):
        value = report.get(key)
        if isinstance(value, str) and value:
            names.add(value)
    return names


def _pack_manifest_path(pack: Path) -> Path:
    manifest = pack / "manifest.json"
    if not manifest.is_file():
        raise ScenarioError(f"pack {pack} has no manifest.json")
    return manifest


def _pack_jsonl(pack: Path) -> list[Path]:
    facts = sorted((pack / "source_facts").glob("*.jsonl"))
    if not facts:
        raise ScenarioError(f"pack {pack} declares no source_facts/*.jsonl")
    return facts


def _load_manifest(pack: Path) -> dict[str, Any]:
    data = json.loads(_pack_manifest_path(pack).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ScenarioError(f"pack {pack}: manifest.json is not an object")
    return data


def _matching_keys(manifest: dict[str, Any], hints: Iterable[str]) -> list[str]:
    return sorted(
        key for key in manifest if any(hint in key.lower() for hint in hints)
    )


def mutate_missing_tu(pack: Path) -> str:
    victim = _pack_jsonl(pack)[0]
    victim.unlink()
    return f"deleted source_facts/{victim.name}"


def mutate_corrupt_pack(pack: Path) -> str:
    victim = _pack_jsonl(pack)[0]
    victim.write_text("{ this is not JSONL\n", encoding="utf-8")
    return f"corrupted source_facts/{victim.name}"


def mutate_wrong_llvm_major(pack: Path) -> str:
    manifest_path = _pack_manifest_path(pack)
    manifest = _load_manifest(pack)
    keys = [
        key
        for key in _matching_keys(manifest, VERSION_KEY_HINTS)
        if key not in PACK_SCHEMA_KEYS
    ]
    bumped = []
    for key in keys:
        value = manifest[key]
        # An empty identity is no identity: a pack whose producer field is ""
        # cannot be given the "wrong" LLVM major, so it must not count as a
        # successful mutation.
        if isinstance(value, str) and value.strip():
            manifest[key] = f"999{value}" if value[0].isdigit() else f"{value}-llvm999"
            bumped.append(key)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, int):
            manifest[key] = value + 999
            bumped.append(key)
    if not bumped:
        # Not a skip. A pack carrying no producer identity at all cannot be
        # rejected for carrying the wrong one, which is the hazard itself --
        # and reporting a pass here would claim a compatibility binding that
        # was never exercised.
        raise ScenarioError(
            "pack manifest records no populated plugin/LLVM identity to mutate "
            "(the pack's own format version is not one); keys are "
            + ", ".join(sorted(manifest))
        )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return "bumped LLVM/plugin version in " + ", ".join(bumped)


def mutate_empty_public_roots(pack: Path) -> str:
    manifest_path = _pack_manifest_path(pack)
    manifest = _load_manifest(pack)
    keys = [
        key
        for key in _matching_keys(manifest, PUBLIC_ROOT_KEY_HINTS)
        if isinstance(manifest[key], list)
    ]
    if not keys:
        raise ScenarioError(
            "pack manifest declares no public header roots to empty; keys are "
            + ", ".join(sorted(manifest))
        )
    for key in keys:
        manifest[key] = []
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return "emptied " + ", ".join(keys)


#: (id, description, pack mutation, header mutation). Exactly one of the two
#: mutations is set per case.
NEGATIVE_CASES: tuple[tuple[str, str, Callable[[Path], str] | None, bool], ...] = (
    ("pack-removed", "the pack is not shipped at all", None, False),
    ("stale-source-digest", "a header edited after collection", None, True),
    ("missing-tu", "one translation unit's facts are gone", mutate_missing_tu, False),
    ("wrong-llvm-major", "a pack from a different LLVM major", mutate_wrong_llvm_major, False),
    ("empty-public-roots", "a pack declaring no public headers", mutate_empty_public_roots, False),
    ("corrupted-pack", "invalid JSONL in the pack", mutate_corrupt_pack, False),
)


def run_negative_case(
    case_id: str,
    description: str,
    mutate: Callable[[Path], str] | None,
    stale_header: bool,
    *,
    bundle: "Bundle",
    workdir: Path,
) -> dict[str, Any]:
    case_dir = workdir / case_id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    header = case_dir / bundle.header.name
    shutil.copy2(bundle.header, header)
    pack: Path | None = case_dir / "abicheck_inputs"
    shutil.copytree(bundle.pack, pack)

    detail = ""
    if case_id == "pack-removed":
        shutil.rmtree(pack)
        pack = None
        detail = "no --build-info supplied"
    if stale_header:
        with header.open("a", encoding="utf-8") as fh:
            fh.write("\n// edited after the facts pack was collected\n")
        detail = "appended a line to the public header"
    if mutate is not None:
        detail = mutate(pack)  # type: ignore[arg-type]

    outcome = run_compare(
        baseline=bundle.baseline,
        binary=bundle.binary,
        header=header,
        pack=pack,
        out=case_dir / "report.json",
    )
    channel = outcome.rejection_channel()
    return {
        "case": case_id,
        "description": description,
        "mutation": detail,
        "rejected": channel is not None,
        "rejection_channel": channel,
        "exit_code": outcome.exit_code,
        "depth_errors": outcome.depth_errors,
    }


@dataclass
class Bundle:
    """The evidence this job is allowed to see, and nothing else."""

    pack: Path
    binary: Path
    header: Path
    baseline: Path

    @classmethod
    def load(cls, root: Path) -> "Bundle":
        pack = root / "abicheck_inputs"
        binary = _sole(root / "lib", "*.so")
        header = _sole(root / "include", "*.h")
        baseline = _sole(root / "baseline", "*.json")
        if not pack.is_dir():
            raise ScenarioError(f"bundle {root} has no abicheck_inputs/ pack")
        return cls(pack=pack, binary=binary, header=header, baseline=baseline)


def _sole(directory: Path, pattern: str) -> Path:
    if not directory.is_dir():
        raise ScenarioError(f"bundle is missing {directory.name}/")
    matches = sorted(directory.rglob(pattern))
    if len(matches) != 1:
        raise ScenarioError(
            f"expected exactly one {pattern} under {directory}, found {len(matches)}"
        )
    return matches[0]


def run(bundle_root: Path, replay_report: Path, workdir: Path, workspace: Path) -> dict[str, Any]:
    bundle = Bundle.load(bundle_root)
    workdir.mkdir(parents=True, exist_ok=True)
    assertions: list[dict[str, Any]] = []

    def record(name: str, errors: list[str], **extra: Any) -> None:
        assertions.append({"assertion": name, "passed": not errors, "errors": errors, **extra})

    record("clean-workspace", assert_clean_workspace(workspace))

    reuse_report_path = workdir / "reuse-report.json"
    reuse = run_compare(
        baseline=bundle.baseline,
        binary=bundle.binary,
        header=bundle.header,
        pack=bundle.pack,
        out=reuse_report_path,
    )
    record(
        "reuse-compare-proves-source-depth",
        list(reuse.depth_errors)
        + ([] if reuse.exit_code in VERDICT_EXIT_CODES else [f"operational exit {reuse.exit_code}"]),
        exit_code=reuse.exit_code,
    )

    replay: dict[str, Any] | None = None
    if replay_report.is_file() and replay_report.stat().st_size:
        loaded = json.loads(replay_report.read_text(encoding="utf-8"))
        replay = loaded if isinstance(loaded, dict) else None

    if replay is None:
        # Never a skip: with no replay report there is nothing to be equal
        # to, and reporting "agrees" would be reporting silence as agreement.
        for name in ("same-findings-as-replay", "same-effective-depth", "same-target-accounting"):
            record(name, [f"replay report {replay_report} is missing or unreadable"])
    else:
        mine = normalized_findings(reuse.report, replay)
        theirs = normalized_findings(replay, reuse.report)
        errors = []
        if mine != theirs:
            errors.append(
                f"reuse-only={sorted((mine - theirs).elements())!r}, "
                f"replay-only={sorted((theirs - mine).elements())!r}"
            )
        record("same-findings-as-replay", errors, findings=len(list(mine.elements())))

        mine_depth, their_depth = effective_depth(reuse.report), effective_depth(replay)
        record(
            "same-effective-depth",
            []
            if mine_depth == their_depth == "source"
            else [f"reuse={mine_depth!r}, replay={their_depth!r}, expected both 'source'"],
        )

        mine_targets, their_targets = target_accounting(reuse.report), target_accounting(replay)
        if not mine_targets and not their_targets:
            targets_errors = [
                "neither report names any target; equal silence is not equal accounting"
            ]
        elif mine_targets != their_targets:
            targets_errors = [
                f"reuse-only={sorted(mine_targets - their_targets)!r}, "
                f"replay-only={sorted(their_targets - mine_targets)!r}"
            ]
        else:
            targets_errors = []
        record("same-target-accounting", targets_errors, targets=sorted(mine_targets))

    negatives = []
    for case_id, description, mutate, stale_header in NEGATIVE_CASES:
        try:
            result = run_negative_case(
                case_id, description, mutate, stale_header, bundle=bundle, workdir=workdir
            )
        except ScenarioError as exc:
            result = {
                "case": case_id,
                "description": description,
                "mutation": None,
                "rejected": False,
                "rejection_channel": None,
                "harness_error": str(exc),
            }
        negatives.append(result)
        record(
            f"rejects/{case_id}",
            []
            if result["rejected"]
            else [
                result.get("harness_error")
                or f"{description}: produced a clean complete source-depth report"
            ],
            rejection_channel=result.get("rejection_channel"),
        )

    return {
        "scenario": "plugin-pack-reuse",
        "bundle": str(bundle_root),
        "assertions": assertions,
        "negative_cases": negatives,
        "passed": all(item["passed"] for item in assertions),
    }


def render_summary(receipt: dict[str, Any]) -> str:
    """A markdown table of every assertion, for the job summary.

    Rendered here rather than in the workflow so the shape stays testable
    and the status step stays a plain `cat` -- a nested heredoc inside a
    brace group redirected to $GITHUB_STEP_SUMMARY is exactly the kind of
    shell that breaks silently and reports nothing.
    """
    lines = [
        "### Plugin-pack reuse in a clean job (best-effort, non-blocking)",
        "",
        "| Assertion | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for item in receipt["assertions"]:
        mark = "\u2705" if item["passed"] else "\u274c"
        detail = item.get("rejection_channel") or "; ".join(item["errors"]) or ""
        lines.append(f"| `{item['assertion']}` | {mark} | " + detail.replace("|", "\\|") + " |")
    lines += [
        "",
        "Overall: "
        + ("\u2705 all nine assertions hold" if receipt["passed"] else "\u274c see the table above"),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True,
                        help="downloaded stage-2 bundle: abicheck_inputs/, lib/, include/, baseline/")
    parser.add_argument("--replay-report", type=Path, required=True,
                        help="the l4-clang-replay report to agree with")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path("."),
                        help="the job checkout, asserted to carry no source tree")
    parser.add_argument("--out", type=Path, help="write the receipt here")
    parser.add_argument("--summary", type=Path, help="write a markdown assertion table here")
    args = parser.parse_args(argv)

    try:
        receipt = run(args.bundle, args.replay_report, args.workdir, args.workspace)
    except ScenarioError as exc:
        print(f"plugin-pack-reuse: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(receipt, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(render_summary(receipt), encoding="utf-8")
    print(text)

    for item in receipt["assertions"]:
        status = "PASS" if item["passed"] else "FAIL"
        print(f"{status} {item['assertion']}"
              + ("" if item["passed"] else ": " + "; ".join(item["errors"])), file=sys.stderr)
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
