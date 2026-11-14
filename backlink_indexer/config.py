from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Pick a writable data directory. Never raise during import."""
    preferred = Path(os.environ.get("APP_DATA_DIR", ROOT / "data")).resolve()
    fallback = Path("/tmp/backlink-indexer-data")
    for path in (preferred, fallback, Path("/tmp")):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except Exception:
            continue
    return Path("/tmp")


DATA_DIR = _data_dir()

DB_PATH = DATA_DIR / "app.db"
SA_PATH = DATA_DIR / "google-service-account.json"

DEFAULT_TARGET_DOMAIN = os.environ.get("DEFAULT_TARGET_DOMAIN", "feetfinder.com")
DEFAULT_YANDEX_VERIFICATION = "132993a7deb9e806"
PUBLIC_BASE_URL = os.environ.get("APP_PUBLIC_BASE_URL", "").rstrip("/")
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))

USER_AGENT = (
    "BacklinkIndexer/1.0 (self-hosted precheck; not Googlebot; "
    "+https://local/backlink-indexer)"
)

# Conservative fetch defaults so we do not hammer third-party sites.
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "20"))
FETCH_CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "4"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(2_000_000)))
