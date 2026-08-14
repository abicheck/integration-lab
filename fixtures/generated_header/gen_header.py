#!/usr/bin/env python3
"""Generate a tiny public C++ header declaring one function per name given
on argv, inside `namespace scenario { ... }`.

This is the fixture generator behind the `generated_header_removed_function`
scenario (scenarios/manifest.yaml): unlike every other fixture pair under
fixtures/, `v1/lib.h`/`v2/lib.h` here are NOT checked-in source files -- a
Bazel `genrule` in each package invokes this script at build time, so the
"public header" abicheck reads for `--header new=...` is itself a build
output living under `bazel-bin/`, not a source-tree path. This validates
that abicheck's header ingestion (and, via abi-scan.yml's own
`--public-header-dir`, its public-header provenance classification) works
correctly against a Bazel-generated header, not just a committed one --
the architecture review's own "generated headers" gap (see README.md's
"Known limitations" section, which this fixture closes).

Usage: gen_header.py <out.h> <function-name>...
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {argv[0]} <out.h> <function-name>...", file=sys.stderr)
        return 2

    out_path = argv[1]
    functions = argv[2:]

    lines = [
        "// GENERATED FILE -- produced by fixtures/generated_header/gen_header.py",
        "// at Bazel build time. Do not hand-edit; do not check in the output.",
        "#pragma once",
        "",
        "namespace scenario {",
        "",
    ]
    for fn in functions:
        lines.append(f"int {fn}(int x);")
    lines.append("")
    lines.append("}  // namespace scenario")
    lines.append("")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
