"""Unit tests for scripts/render_performance_summary.py -- performance.yml's
cold/warm + (PR5) per-profile Markdown summary rendering."""
from __future__ import annotations

import json

from render_performance_summary import render


def _write(tmp_path, data):
    path = tmp_path / "timings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_render_bazel_only_omits_profiles_section(tmp_path):
    path = _write(
        tmp_path,
        {
            "bazel_build_cold_ms": 5000,
            "bazel_build_warm_ms": 800,
            "abicheck_dump_cold_ms": 1200,
            "abicheck_dump_warm_ms": 300,
            "sha": "abc123",
            "scanner_sha": "6fb8536",
        },
    )
    text = render(path)
    assert "5,000 ms" in text
    assert "6.25×" in text
    assert "Scanner SHA: `6fb8536`" in text
    assert "Per-profile build" not in text


def test_render_profiles_only_shows_dashes_for_missing_bazel_fields(tmp_path):
    path = _write(
        tmp_path,
        {
            "sha": "abc123",
            "scanner_sha": "6fb8536",
            "profiles": [
                {"profile_id": "linux-x86_64-gcc14-cxx17-cmake-ninja", "build_ms": 2000, "dump_ms": 400},
                {"profile_id": "linux-x86_64-gcc14-cxx17-make-bear", "build_ms": None, "dump_ms": None},
            ],
        },
    )
    text = render(path)
    assert "Per-profile build + dump" in text
    assert "linux-x86_64-gcc14-cxx17-cmake-ninja" in text
    assert "2,000 ms" in text
    assert "400 ms" in text
    assert "linux-x86_64-gcc14-cxx17-make-bear" in text
    # The Bazel-specific table still renders (with placeholder dashes)
    # rather than being silently omitted.
    assert "bazel build //:math" in text
    assert text.count("—") >= 2  # bazel row placeholders


def test_render_empty_profiles_list_omits_section(tmp_path):
    path = _write(tmp_path, {"sha": "abc123", "profiles": []})
    text = render(path)
    assert "Per-profile build" not in text


def test_render_missing_scanner_sha_omits_line(tmp_path):
    path = _write(tmp_path, {"sha": "abc123"})
    text = render(path)
    assert "Scanner SHA" not in text


def test_render_ignores_non_dict_profile_entries(tmp_path):
    path = _write(tmp_path, {"sha": "abc123", "profiles": ["not-a-dict", {"profile_id": "x", "build_ms": 1, "dump_ms": 2}]})
    text = render(path)
    assert "| `x` |" in text
