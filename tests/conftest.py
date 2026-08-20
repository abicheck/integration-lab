"""pytest bootstrap: scripts/*.py import each other as plain top-level
modules (e.g. emit_capability_receipt.py does `from capability_receipts
import ...`), matching the pattern gen_capability_gaps.py already uses
(`import check_capability_matrix`) when run directly as
`python3 scripts/foo.py`. Adding scripts/ to sys.path here reproduces that
same run-as-a-script import context for pytest, instead of requiring
scripts/ to be turned into an installable package.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
