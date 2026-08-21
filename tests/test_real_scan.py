"""Unit tests for ci/real_scan.py's pure helpers (compile-db filtering and
verdict mapping) -- the parts of the real-scanner integration
(roadmap.md item 1) that don't require actually invoking `abicheck` or a
C++ toolchain. See tests/test_check_profile.py for the fail-closed
behavior when a baseline predates this integration, and this repo's own
PR history for the end-to-end verification against a real build.
"""
from __future__ import annotations

import json

import pytest

from real_scan import RealScanError, filter_compile_db_for_target, map_verdict, target_source_suffix


def _write_db(tmp_path, entries):
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps(entries))
    return path


def test_target_source_suffix_known_targets():
    assert target_source_suffix("math") == "src/math.cc"
    assert target_source_suffix("strings") == "strings_lib/src/strings.cc"


def test_target_source_suffix_unknown_target_fails_closed():
    with pytest.raises(RealScanError, match="no known source file"):
        target_source_suffix("nonexistent")


def test_filter_compile_db_for_target_selects_only_matching_entries(tmp_path):
    db = _write_db(tmp_path, [
        {"file": "/repo/src/math.cc", "command": "g++ -c math.cc"},
        {"file": "/repo/strings_lib/src/strings.cc", "command": "g++ -c strings.cc"},
        {"file": "/repo/consumer/app.cc", "command": "g++ -c app.cc"},
    ])
    out = tmp_path / "filtered.json"
    filter_compile_db_for_target(db, "math", out)
    filtered = json.loads(out.read_text())
    assert len(filtered) == 1
    assert filtered[0]["file"] == "/repo/src/math.cc"


def test_filter_compile_db_for_target_no_match_fails_closed(tmp_path):
    db = _write_db(tmp_path, [{"file": "/repo/consumer/app.cc", "command": "g++ -c app.cc"}])
    with pytest.raises(RealScanError, match="has no entry for target"):
        filter_compile_db_for_target(db, "math", tmp_path / "filtered.json")


def test_filter_compile_db_for_target_missing_file_fails_closed(tmp_path):
    with pytest.raises(RealScanError, match="no compile database"):
        filter_compile_db_for_target(tmp_path / "missing.json", "math", tmp_path / "filtered.json")


def test_filter_compile_db_for_target_invalid_json_fails_closed(tmp_path):
    db = tmp_path / "compile_commands.json"
    db.write_text("not json")
    with pytest.raises(RealScanError, match="not valid JSON"):
        filter_compile_db_for_target(db, "math", tmp_path / "filtered.json")


def test_filter_compile_db_for_target_not_a_list_fails_closed(tmp_path):
    db = tmp_path / "compile_commands.json"
    db.write_text(json.dumps({"file": "not-a-list"}))
    with pytest.raises(RealScanError, match="not a JSON array"):
        filter_compile_db_for_target(db, "math", tmp_path / "filtered.json")


@pytest.mark.parametrize("verdict,expected", [
    ("NO_CHANGE", "NO_CHANGE"),
    ("COMPATIBLE", "COMPATIBLE"),
    ("COMPATIBLE_WITH_RISK", "COMPATIBLE"),
    ("API_BREAK", "BREAKING"),
    ("BREAKING", "BREAKING"),
    ("NOT_COMPARABLE", "NOT_COMPARABLE"),
])
def test_map_verdict_known_values(verdict, expected):
    mapped = map_verdict(verdict)
    assert mapped["verdict"] == expected
    assert mapped["unmapped_abicheck_verdict"] is None


def test_map_verdict_unknown_value_fails_closed():
    mapped = map_verdict("SOME_FUTURE_VERDICT")
    assert mapped["verdict"] == "NOT_COMPARABLE"
    assert mapped["unmapped_abicheck_verdict"] == "SOME_FUTURE_VERDICT"


def test_map_verdict_missing_value_fails_closed():
    mapped = map_verdict(None)
    assert mapped["verdict"] == "NOT_COMPARABLE"
    assert mapped["unmapped_abicheck_verdict"] is None
