from __future__ import annotations

import httpx

from ..config import USER_AGENT

DEFAULT_HUB = "https://pubsubhubbub.appspot.com/"
BING_SITEMAP_PING = "https://www.bing.com/ping"


async def ping_websub(
    client: httpx.AsyncClient,
    feed_url: str,
    hub_url: str = DEFAULT_HUB,
) -> dict:
    if not feed_url:
        return {"ok": False, "detail": "Public base URL is not set, so the feed cannot be pinged."}
    try:
        resp = await client.post(
            hub_url,
            data={"hub.mode": "publish", "hub.url": feed_url},
            timeout=20,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except Exception as exc:
        return {"ok": False, "detail": f"WebSub ping failed: {exc}", "hub": hub_url}
    ok = resp.status_code in {200, 202, 204}
    body = (resp.text or "")[:400]
    detail = f"HTTP {resp.status_code}"
    if body:
        detail = f"{detail}: {body}"
    return {"ok": ok, "status": resp.status_code, "detail": detail, "hub": hub_url, "feed": feed_url}


async def ping_bing_sitemap(client: httpx.AsyncClient, sitemap_url: str) -> dict:
    if not sitemap_url:
        return {"ok": False, "detail": "Public base URL is not set, so the sitemap cannot be pinged."}
    try:
        resp = await client.get(
            BING_SITEMAP_PING,
            params={"sitemap": sitemap_url},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    except Exception as exc:
        return {"ok": False, "detail": f"Bing sitemap ping failed: {exc}"}
    ok = resp.status_code in {200, 202, 204}
    return {
        "ok": ok,
        "status": resp.status_code,
        "detail": f"HTTP {resp.status_code}",
        "sitemap": sitemap_url,
    }
