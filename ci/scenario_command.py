"""Fail-closed subprocess handling shared by executable scenario runners."""
from __future__ import annotations

import subprocess
from pathlib import Path


VERDICT_EXIT_CODES = frozenset({0, 1, 2, 4})


def run_command(argv: list[str], *, verdict_report: Path | None = None) -> None:
    """Run a command, accepting verdict exits only for a fresh report.

    ABICheck uses 1/2/4 for valid gated results, while operational failures
    use other statuses such as 64. Removing the expected report first prevents
    a rerun from validating JSON left by a previous successful invocation.
    """
    if verdict_report is not None:
        verdict_report.unlink(missing_ok=True)
    result = subprocess.run(argv)
    allowed = VERDICT_EXIT_CODES if verdict_report is not None else {0}
    if result.returncode not in allowed:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}")
    if verdict_report is not None and not verdict_report.is_file():
        raise RuntimeError(f"command produced no report {verdict_report}: {' '.join(argv)}")
