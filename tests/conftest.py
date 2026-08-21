"""pytest bootstrap: scripts/*.py import each other as plain top-level
modules (e.g. emit_capability_receipt.py does `from capability_receipts
import ...`), matching the pattern gen_capability_gaps.py already uses
(`import check_capability_matrix`) when run directly as
`python3 scripts/foo.py`. Adding scripts/ to sys.path here reproduces that
same run-as-a-script import context for pytest, instead of requiring
scripts/ to be turned into an installable package.

PR1 (multi-build-system integration) adds ci/*.py and ci/backends/*.py,
which follow the identical run-as-a-script import convention (see
ci/backends/__init__.py's own docstring) -- ci/ and ci/backends/ are added
to sys.path here the same way scripts/ already is.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CI_DIR = REPO_ROOT / "ci"
CI_BACKENDS_DIR = CI_DIR / "backends"

for _dir in (SCRIPTS_DIR, CI_DIR, CI_BACKENDS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))
