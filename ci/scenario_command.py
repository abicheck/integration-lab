"""Fail-closed subprocess handling shared by executable scenario runners."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


VERDICT_EXIT_CODES = frozenset({0, 1, 2, 4})


def run_command(
    argv: list[str], *, verdict_report: Path | None = None, isolated_cache: bool = False
) -> None:
    """Run a command, accepting verdict exits only for a fresh report.

    ABICheck uses 1/2/4 for valid gated results, while operational failures
    use other statuses such as 64. Removing the expected report first prevents
    a rerun from validating JSON left by a previous successful invocation.
    """
    if verdict_report is not None:
        verdict_report.unlink(missing_ok=True)
    if isolated_cache:
        # abicheck's on-disk snapshot cache keys a dump on the binary and
        # its headers but not on a sibling `.pyi` stub, so two sides built
        # from one identical extension binary with different stubs share a
        # cache entry and the second side silently reuses the first side's
        # Python API (see UPSTREAM_TO_ABICHECK.md, "snapshot cache ignores
        # .pyi stubs"). A fresh cache root per invocation keeps each side's
        # facts its own.
        with tempfile.TemporaryDirectory(prefix="abicheck-cache-") as cache:
            result = subprocess.run(argv, env={**os.environ, "XDG_CACHE_HOME": cache})
    else:
        result = subprocess.run(argv)
    allowed = VERDICT_EXIT_CODES if verdict_report is not None else {0}
    if result.returncode not in allowed:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}")
    if verdict_report is not None and not verdict_report.is_file():
        raise RuntimeError(f"command produced no report {verdict_report}: {' '.join(argv)}")
