"""Yandex Webmaster recrawl queue.

POST https://api.webmaster.yandex.net/v4/user/{user-id}/hosts/{host-id}/recrawl/queue
Authorization: OAuth <token>
Body: {"url": "<url on that verified host>"}

A 202 means Yandex queued a recrawl. It does not mean the URL is indexed.
Daily quota is read from the documented quota endpoint and is not exceeded.
"""

from __future__ import annotations

import httpx

from ..config import USER_AGENT

API = "https://api.webmaster.yandex.net/v4"


def _headers(token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


async def fetch_quota(
    client: httpx.AsyncClient,
    token: str,
    user_id: str,
    host_id: str,
) -> dict:
    url = f"{API}/user/{user_id}/hosts/{host_id}/recrawl/quota"
    try:
        response = await client.get(url, headers=_headers(token), timeout=30)
    except Exception as exc:
        return {"ok": False, "detail": f"Yandex recrawl quota request failed: {exc}"}
    body = (response.text or "")[:500]
    if response.status_code != 200:
        return {"ok": False, "status": response.status_code, "detail": f"Yandex quota HTTP {response.status_code}: {body}"}
    try:
        data = response.json()
    except Exception:
        return {"ok": False, "status": response.status_code, "detail": f"Yandex quota was not JSON: {body}"}
    remainder = data.get("quota_remainder")
    if remainder is None:
        return {"ok": False, "status": response.status_code, "detail": f"Yandex quota response had no remainder: {body}"}
    try:
        remainder = int(remainder)
    except (TypeError, ValueError):
        return {"ok": False, "detail": f"Yandex quota remainder was not a number: {body}"}
    return {"ok": True, "remainder": remainder, "detail": body}


async def recrawl_url(
    client: httpx.AsyncClient,
    token: str,
    user_id: str,
    host_id: str,
    url: str,
) -> dict:
    """Ask Yandex to recrawl one URL. Never reports it as indexed."""
    if not token or not user_id or not host_id:
        return {
            "ok": False,
            "state": "skipped",
            "status": None,
            "detail": "Yandex Webmaster OAuth token, user id, and host id are required for recrawl.",
        }
    endpoint = f"{API}/user/{user_id}/hosts/{host_id}/recrawl/queue"
    try:
        response = await client.post(
            endpoint,
            headers=_headers(token),
            json={"url": url},
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "state": "rejected", "status": None, "detail": f"Yandex recrawl request failed: {exc}"}
    body = (response.text or "")[:500]
    detail = f"Yandex recrawl HTTP {response.status_code}"
    if body:
        detail = f"{detail}: {body}"
    # 202 = queued. 409 = already in the recrawl queue. Neither is an index.
    if response.status_code in {200, 202, 409}:
        return {
            "ok": True,
            "state": "accepted",
            "status": response.status_code,
            "detail": detail + " Recrawl queued. This is not an index confirmation.",
        }
    return {"ok": False, "state": "rejected", "status": response.status_code, "detail": detail}
