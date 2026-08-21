#!/usr/bin/env python3
"""Stage one profile's build result into the canonical
`abicheck-build-<profile-id>/` output directory (PR1 item 6):

    abicheck-build-<profile-id>/
      build-output.json      -- see ci/schemas/build-output.schema.json
      artifacts/lib/*         -- staged shared libraries
      artifacts/bin/*         -- staged executables
      headers/...             -- copies of every declared header root,
                                  under the SAME relative path they have in
                                  the repo (e.g. headers/include/abicheck_lab/math.h)
      evidence/...            -- backend-specific build evidence (a copy of
                                  compile_commands.json when the backend has
                                  one, a bazel cquery capture, etc.)
      provenance/build-system.json -- resolved compiler/generator identity
                                  (BuildBackend.describe()'s own output)

This module exposes `stage_profile()` as an importable function (used by
ci/run_profile.py and by tests/test_emit_build_output.py) as well as a
standalone CLI that builds a named profile end-to-end (verify_environment
-> clean -> build -> collect_evidence -> stage -> emit) for local use:

    python3 ci/emit_build_output.py --profile-id linux-x86_64-gcc14-cxx17-cmake-ninja

Nothing here is GitHub-Actions specific, same as ci/backends/ -- see that
package's own docstring.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "ci"
BACKENDS_DIR = CI_DIR / "backends"
for _p in (str(CI_DIR), str(BACKENDS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backends import build_backend  # noqa: E402
from backends.base import BuildBackend, BuildResult, TargetResult  # noqa: E402


def load_profiles(profiles_path: Path) -> Dict[str, Dict[str, Any]]:
    import yaml

    with open(profiles_path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return {p["id"]: p for p in doc.get("profiles", [])}


def _git_sha(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        sha = proc.stdout.strip()
        return sha if proc.returncode == 0 and sha else "unknown"
    except FileNotFoundError:
        return "unknown"


def _copy_header_root(repo_root: Path, header_root: str, dest_root: Path) -> Optional[str]:
    """Copy one profile.header_roots entry into dest_root. Returns a
    diagnostic string if the declared root doesn't exist under repo_root
    (a profiles.yaml typo or a renamed/removed header directory) so the
    caller can surface it in build-output.json's diagnostics instead of
    silently staging fewer headers than declared -- see select_profiles.py's
    fail-closed-on-unknown-reference precedent for why a declared-but-
    missing root must not pass silently here either.
    """
    src = repo_root / header_root
    if not src.exists():
        return f"header_roots entry {header_root!r} does not exist under {repo_root}"
    dest = dest_root / header_root
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)
    return None


def _copy_evidence(backend_evidence: Dict[str, Any], dest_dir: Path) -> Dict[str, Any]:
    """Copy any file-shaped evidence (e.g. compile_commands.json) into
    dest_dir and rewrite the evidence dict's own path to be relative to the
    staged output directory, so the staged directory is self-contained
    (usable after the original build tree is gone, e.g. once uploaded as a
    CI artifact and downloaded elsewhere).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = dict(backend_evidence)
    for key in ("compile_commands_path",):
        src_str = backend_evidence.get(key)
        if not src_str:
            continue
        src = Path(src_str)
        if src.is_file():
            dest = dest_dir / src.name
            shutil.copy2(src, dest)
            result[key] = f"evidence/{src.name}"
    label_kind = backend_evidence.get("label_kind")
    if label_kind:
        (dest_dir / "bazel-label-kind.txt").write_text(label_kind, encoding="utf-8")
        result.pop("label_kind", None)
        result["label_kind_file"] = "evidence/bazel-label-kind.txt"
    return result


def stage_profile(
    profile: Dict[str, Any],
    repo_root: Path,
    build_result: BuildResult,
    backend: BuildBackend,
    out_dir: Path,
) -> Dict[str, Any]:
    """Stage build_result (already produced by backend.build()) into
    out_dir, following the canonical shape documented in this module's
    docstring. Returns the build-output.json document (also written to
    out_dir/build-output.json).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = out_dir / "artifacts"
    headers_dir = out_dir / "headers"
    evidence_dir = out_dir / "evidence"
    provenance_dir = out_dir / "provenance"

    stage_manifest = backend.stage(build_result, artifacts_dir)

    header_diagnostics = []
    for header_root in profile.get("header_roots", []):
        missing = _copy_header_root(repo_root, header_root, headers_dir)
        if missing:
            header_diagnostics.append(missing)
    header_roots_staged = [
        f"headers/{root}" for root in profile.get("header_roots", []) if (repo_root / root).exists()
    ]

    raw_evidence = backend.collect_evidence(build_result)
    evidence_summary = _copy_evidence(raw_evidence, evidence_dir)

    provenance_dir.mkdir(parents=True, exist_ok=True)
    describe = backend.describe()
    (provenance_dir / "build-system.json").write_text(
        json.dumps(describe, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    targets_doc: Dict[str, Any] = {}
    for name, target in build_result.targets.items():
        staged = stage_manifest.get(name, {})
        targets_doc[name] = {
            "kind": target.kind,
            "built": target.built,
            "path": staged.get("path"),
            "sha256": target.sha256,
            "size_bytes": target.size_bytes,
        }

    compiler = dict(profile.get("compiler", {}))
    compiler["cc_version"] = backend._tool_version(compiler.get("cc", "")) if compiler.get("cc") else None
    compiler["cxx_version"] = backend._tool_version(compiler.get("cxx", "")) if compiler.get("cxx") else None

    doc: Dict[str, Any] = {
        "schema_version": 1,
        "project": {"name": "abicheck-bazel-lab"},
        "git": {"sha": _git_sha(repo_root), "ref": None},
        "profile": {
            "id": profile["id"],
            "backend": profile["backend"],
            "contract": bool(profile.get("contract", False)),
            "root": profile["root"],
            "generator": profile.get("generator"),
        },
        "compiler": compiler,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(build_result.duration_s, 3),
        "targets": targets_doc,
        "header_roots": header_roots_staged,
        "evidence": {"dir": "evidence", "backend_evidence": evidence_summary},
        "provenance": {"dir": "provenance"},
        "diagnostics": list(build_result.diagnostics) + header_diagnostics,
        "success": bool(build_result.success),
    }

    (out_dir / "build-output.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return doc


def build_and_stage(
    profile_id: str,
    profiles_path: Path = CI_DIR / "profiles.yaml",
    repo_root: Path = REPO_ROOT,
    out_root: Optional[Path] = None,
) -> Dict[str, Any]:
    profiles = load_profiles(profiles_path)
    if profile_id not in profiles:
        raise KeyError(f"unknown profile id: {profile_id!r}")
    profile = profiles[profile_id]

    backend = build_backend(profile, repo_root)
    env = backend.verify_environment()
    if not env.ok:
        raise RuntimeError(
            f"profile {profile_id!r} environment check failed: missing {env.missing}"
        )
    backend.clean()
    result = backend.build()

    out_dir = (out_root or repo_root) / f"abicheck-build-{profile_id}"
    return stage_profile(profile, repo_root, result, backend, out_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profiles-file", type=Path, default=CI_DIR / "profiles.yaml")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None, help="parent directory for abicheck-build-<id>/")
    parser.add_argument("--validate", action="store_true", help="also run ci/validate_build_output.py on the result")
    args = parser.parse_args(argv)

    try:
        doc = build_and_stage(
            args.profile_id,
            profiles_path=args.profiles_file,
            repo_root=args.repo_root,
            out_root=args.out_dir,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        print(f"emit_build_output: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"profile_id": args.profile_id, "success": doc["success"]}))

    if args.validate:
        from validate_build_output import validate_file

        out_dir = (args.out_dir or args.repo_root) / f"abicheck-build-{args.profile_id}"
        errors = validate_file(out_dir / "build-output.json")
        if errors:
            for err in errors:
                print(f"schema violation: {err}", file=sys.stderr)
            return 1

    return 0 if doc["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
