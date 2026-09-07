"""fixtures/generated_header/ has TWO declarations of the same function
list: fixtures/generated_header/v{1,2}/BUILD.bazel's own genrule `cmd`
(Bazel's own generation path, unchanged by this file) and
fixtures/generated_header/v{1,2}/header_functions.txt (read by
buildsystems/cmake/fixtures/CMakeLists.txt and buildsystems/make/fixtures/
Makefile's own generic generation step -- see either file's own comment).
These two are NOT one source of truth mechanically (BUILD.bazel's own
`cmd` is a shell string, not a Bazel `data` dependency on the .txt file),
so nothing stops them drifting apart silently -- this test is that check.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "fixtures" / "generated_header"

# Matches gen_header.py $@ kept_function removed_function -- the function
# name list is everything after the two positional args ($(location ...)
# and $@) on the genrule's own `cmd` line.
_CMD_RE = re.compile(r'cmd\s*=\s*"\$\(location [^)]+\)\s+\$@\s+([^"]*)"')


def _bazel_genrule_functions(version: str) -> list[str]:
    text = (FIXTURE_ROOT / version / "BUILD.bazel").read_text(encoding="utf-8")
    match = _CMD_RE.search(text)
    assert match, f"could not find gen_lib_h's cmd= in {version}/BUILD.bazel"
    return match.group(1).split()


def _header_functions_txt(version: str) -> list[str]:
    text = (FIXTURE_ROOT / version / "header_functions.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_v1_header_functions_match_the_bazel_genrule():
    assert _header_functions_txt("v1") == _bazel_genrule_functions("v1")


def test_v2_header_functions_match_the_bazel_genrule():
    assert _header_functions_txt("v2") == _bazel_genrule_functions("v2")


def test_v2_dropped_exactly_removed_function():
    """The scenario's own claim (scenarios/manifest.yaml: v2 drops
    "removed_function" from the declared list) -- checked directly against
    both declarations, not assumed.
    """
    v1 = set(_header_functions_txt("v1"))
    v2 = set(_header_functions_txt("v2"))
    assert v1 - v2 == {"removed_function"}
    assert v2 - v1 == set()
