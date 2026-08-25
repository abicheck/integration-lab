import json

from validate_project_foundation import validate


def _write_fixture(tmp_path, *, targets=("core", "math"), compiler=True):
    config = tmp_path / ".abicheck.yml"
    config.write_text(
        "targets:\n  core: {kind: library}\n  math: {kind: library}\n"
        "profiles:\n  p: {contract: true}\n"
    )
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        "profiles:\n  - id: p\n    compiler: {family: gcc}\n"
        "    targets: {core: core, math: math}\n"
    )
    root = tmp_path / "build"
    root.mkdir()
    compiler_doc = {
        "family": "gcc", "version": "14.2.0", "path": "/usr/bin/g++-14",
        "digest": "sha256:abc", "standard": "c++17", "abi_macros": "__cplusplus 201703L",
    } if compiler else {}
    (root / "build-output.json").write_text(json.dumps({
        "profile": {"id": "p", "compiler": compiler_doc},
        "targets": [{"id": target} for target in targets],
    }))
    return config, profiles, root


def test_matching_foundation_passes(tmp_path):
    assert validate(*_write_fixture(tmp_path)) == []


def test_missing_target_and_provenance_fail(tmp_path):
    errors = validate(*_write_fixture(tmp_path, targets=("core",), compiler=False))
    assert any("target ids differ" in error for error in errors)
    assert any("compiler provenance" in error for error in errors)


def test_source_check_requires_declared_target_pack(tmp_path):
    config, profiles, root = _write_fixture(tmp_path)
    config.write_text(config.read_text().replace(
        "core: {kind: library}",
        "core: {kind: library, checks: [{channel: accepted-main, depth: source}]}",
    ))
    assert any("lacks a declared per-target evidence pack" in error
               for error in validate(config, profiles, root))


def test_missing_or_malformed_receipt_is_reported(tmp_path):
    config, profiles, root = _write_fixture(tmp_path)
    receipt = root / "build-output.json"
    receipt.unlink()
    assert any("cannot load" in error for error in validate(config, profiles, root))
    receipt.write_text("{")
    assert any("cannot load" in error for error in validate(config, profiles, root))
