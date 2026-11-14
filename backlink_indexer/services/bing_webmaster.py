"""Bing Webmaster URL Submission API.

SubmitUrlBatch: https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlBatch
Documented limit: up to 10,000 URLs per day for a site verified in Bing Webmaster
Tools. A 200 response means Bing accepted the submission. It does not mean the
URL is indexed.
"""

from __future__ import annotations

import httpx

from ..config import USER_AGENT
from ..database import get_setting, tx, utcnow

SUBMIT_URL = "https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlBatch"
DAILY_LIMIT = 10000


def _today() -> str:
    return utcnow()[:10]


def quota_remaining() -> int:
    if get_setting("bing_quota_date") != _today():
        return DAILY_LIMIT
    try:
        used = int(get_setting("bing_quota_count") or "0")
    except ValueError:
        used = 0
    return max(0, DAILY_LIMIT - used)


def _consume(count: int = 1) -> None:
    today = _today()
    with tx() as conn:
        row_date = conn.execute("SELECT value FROM settings WHERE key='bing_quota_date'").fetchone()
        row_count = conn.execute("SELECT value FROM settings WHERE key='bing_quota_count'").fetchone()
        current = 0
        if row_date and row_date["value"] == today and row_count and row_count["value"]:
            try:
                current = int(row_count["value"])
            except ValueError:
                current = 0
        new_count = current + count
        for key, value in (("bing_quota_date", today), ("bing_quota_count", str(new_count))):
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )


async def submit_url(
    client: httpx.AsyncClient,
    api_key: str,
    site_url: str,
    url: str,
) -> dict:
    """Submit one URL with SubmitUrlBatch. Never reports the URL as indexed."""
    if not api_key:
        return {"ok": False, "state": "skipped", "status": None, "detail": "Bing Webmaster API key is not set."}
    if quota_remaining() < 1:
        return {
            "ok": False,
            "state": "deferred",
            "status": None,
            "detail": (
                f"Bing Webmaster daily quota of {DAILY_LIMIT} is used. "
                "This URL stays queued and will be submitted on a later day."
            ),
        }
    try:
        response = await client.post(
            SUBMIT_URL,
            params={"apikey": api_key},
            json={"siteUrl": site_url, "urlList": [url]},
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as exc:
        return {"ok": False, "state": "rejected", "status": None, "detail": f"Bing Webmaster request failed: {exc}"}
    body = (response.text or "")[:500]
    detail = f"Bing Webmaster HTTP {response.status_code}"
    if body:
        detail = f"{detail}: {body}"
    if response.status_code == 200:
        _consume(1)
        return {
            "ok": True,
            "state": "accepted",
            "status": response.status_code,
            "detail": detail + " Accepted for submission. This is not an index confirmation.",
        }
    return {"ok": False, "state": "rejected", "status": response.status_code, "detail": detail}
