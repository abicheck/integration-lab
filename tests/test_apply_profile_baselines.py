"""Unit tests for ci/apply_profile_baselines.py (PR4 item 1)."""
from __future__ import annotations

import json

from apply_profile_baselines import apply_one, apply_profile
from check_profile import BASELINE_KIND


def _baseline(profile_id="p1", target="math", symbols=None, headers=None):
    return {
        "schema_version": 1,
        "kind": BASELINE_KIND,
        "profile_id": profile_id,
        "target": target,
        "backend": "bazel",
        "library_filename": "libmath.so",
        "soname": None,
        "dynamic_symbols": symbols if symbols is not None else [{"name": "foo", "type": "T", "size": 1}],
        "public_headers": headers if headers is not None else {"headers/include/api.h": "deadbeef"},
    }


def test_apply_one_writes_new_committed_baseline(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline()))
    committed = tmp_path / "committed" / "math.abicheck.json"

    result = apply_one("p1", "math", raw, committed)
    assert result["status"] == "updated"
    assert committed.is_file()
    written = json.loads(committed.read_text())
    assert written["profile_id"] == "p1"


def test_apply_one_unchanged_when_identical(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline()))
    committed = tmp_path / "math.abicheck.json"
    committed.write_text(json.dumps(_baseline(), indent=2, sort_keys=True) + "\n")

    result = apply_one("p1", "math", raw, committed)
    assert result["status"] == "unchanged"


def test_apply_one_missing_when_raw_absent(tmp_path):
    committed = tmp_path / "math.abicheck.json"
    committed.write_text(json.dumps(_baseline(), indent=2, sort_keys=True) + "\n")
    original = committed.read_text()

    result = apply_one("p1", "math", tmp_path / "does-not-exist.json", committed)
    assert result["status"] == "missing"
    assert committed.read_text() == original  # untouched


def test_apply_one_invalid_wrong_profile_id_leaves_committed_untouched(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline(profile_id="wrong-profile")))
    committed = tmp_path / "math.abicheck.json"
    committed.write_text(json.dumps(_baseline(), indent=2, sort_keys=True) + "\n")
    original = committed.read_text()

    result = apply_one("p1", "math", raw, committed)
    assert result["status"] == "invalid"
    assert "profile_id" in result["detail"]
    assert committed.read_text() == original


def test_apply_one_invalid_empty_dynamic_symbols(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline(symbols=[])))
    committed = tmp_path / "math.abicheck.json"

    result = apply_one("p1", "math", raw, committed)
    assert result["status"] == "invalid"
    assert "dynamic_symbols" in result["detail"]
    assert not committed.exists()


def test_apply_one_invalid_malformed_json(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text("{not valid json")
    committed = tmp_path / "math.abicheck.json"

    result = apply_one("p1", "math", raw, committed)
    assert result["status"] == "invalid"


def test_apply_one_verify_only_reports_verified_and_never_writes(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline()))
    committed = tmp_path / "math.abicheck.json"
    committed.write_text(json.dumps(_baseline(), indent=2, sort_keys=True) + "\n")
    original = committed.read_text()

    result = apply_one("p1", "math", raw, committed, write=False)
    assert result["status"] == "verified"
    assert committed.read_text() == original


def test_apply_one_verify_only_reports_drift_and_never_writes(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_baseline(symbols=[{"name": "bar", "type": "T", "size": 2}])))
    committed = tmp_path / "math.abicheck.json"
    committed.write_text(json.dumps(_baseline(), indent=2, sort_keys=True) + "\n")
    original = committed.read_text()

    result = apply_one("p1", "math", raw, committed, write=False)
    assert result["status"] == "drift"
    assert "does not match" in result["detail"]
    assert committed.read_text() == original


def test_apply_profile_one_bad_target_does_not_block_a_good_one(tmp_path):
    raw_math = tmp_path / "math.json"
    raw_math.write_text(json.dumps(_baseline(target="math")))
    raw_strings = tmp_path / "strings.json"
    raw_strings.write_text(json.dumps(_baseline(target="strings", profile_id="WRONG")))

    committed_dir = tmp_path / "committed"
    result = apply_profile("p1", {"math": raw_math, "strings": raw_strings}, committed_dir)

    assert result["targets"]["math"]["status"] == "updated"
    assert result["targets"]["strings"]["status"] == "invalid"
    assert (committed_dir / "math.abicheck.json").is_file()
    assert not (committed_dir / "strings.abicheck.json").exists()
