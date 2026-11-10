"""Repo-root FastAPI entrypoint for Vercel.

The application package lives in ``backlink-indexer/``. That directory name is
not a valid Python module, so it is added to ``sys.path`` and the app is
imported as ``app.main:app``. Static files and templates stay next to that
package and are resolved from ``__file__`` inside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent / "backlink-indexer"
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from app.main import app as app  # noqa: E402
