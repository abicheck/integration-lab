"""Unit tests for ci/emit_release_manifest.py (PR4 item 2)."""
from __future__ import annotations

import hashlib

from emit_release_manifest import build_manifest


def test_build_manifest_records_digest_and_provenance(tmp_path):
    math_path = tmp_path / "math.abicheck.json"
    math_path.write_text('{"kind": "abicheck.lab-symbol-baseline/v1"}')
    strings_path = tmp_path / "strings.abicheck.json"
    strings_path.write_text('{"kind": "abicheck.lab-symbol-baseline/v1", "target": "strings"}')

    manifest = build_manifest(
        release_tag="v1.2.3",
        source_sha="deadbeef",
        generation=1,
        profile_id="linux-x86_64-gcc14-cxx17-bazel",
        baseline_paths={"math": math_path, "strings": strings_path},
        generated_at="2026-08-21T00:00:00Z",
    )

    assert manifest["release_tag"] == "v1.2.3"
    assert manifest["source_sha"] == "deadbeef"
    assert manifest["generation"] == 1
    assert manifest["profile_id"] == "linux-x86_64-gcc14-cxx17-bazel"
    assert manifest["targets"]["math"]["sha256"] == hashlib.sha256(math_path.read_bytes()).hexdigest()
    assert manifest["targets"]["strings"]["sha256"] == hashlib.sha256(strings_path.read_bytes()).hexdigest()
    assert manifest["kind"] == "abicheck.release-baseline-manifest/v1"
