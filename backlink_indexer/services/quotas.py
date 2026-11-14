"""Daily external API quotas.

Unused calls do not stack above the daily cap. URLs that cannot be submitted
today stay queued for a later day instead of being marked failed.
"""

from __future__ import annotations

from ..database import get_setting, tx, utcnow

LIMITS = {
    "google_indexing": 200,
    "bing_webmaster": 10000,
    "indexnow": 10000,
}


def _today() -> str:
    return utcnow()[:10]


def remaining(name: str) -> int:
    limit = LIMITS[name]
    if get_setting(f"{name}_quota_date") != _today():
        return limit
    try:
        used = int(get_setting(f"{name}_quota_count") or "0")
    except ValueError:
        used = 0
    return max(0, limit - used)


def consume(name: str, count: int = 1) -> bool:
    """Reserve quota. Returns False when today's cap is already used."""
    if count < 1:
        return True
    if remaining(name) < count:
        return False
    today = _today()
    date_key = f"{name}_quota_date"
    count_key = f"{name}_quota_count"
    with tx() as conn:
        row_date = conn.execute("SELECT value FROM settings WHERE key=?", (date_key,)).fetchone()
        row_count = conn.execute("SELECT value FROM settings WHERE key=?", (count_key,)).fetchone()
        current = 0
        if row_date and row_date["value"] == today and row_count and row_count["value"]:
            try:
                current = int(row_count["value"])
            except ValueError:
                current = 0
        if current + count > LIMITS[name]:
            return False
        for key, value in ((date_key, today), (count_key, str(current + count))):
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
    return True


def snapshot() -> list[dict]:
    labels = {
        "google_indexing": "Google Indexing API",
        "bing_webmaster": "Bing Webmaster",
        "indexnow": "IndexNow",
    }
    rows = []
    for name, label in labels.items():
        left = remaining(name)
        rows.append({"name": name, "label": label, "remaining": left, "limit": LIMITS[name]})
    return rows
