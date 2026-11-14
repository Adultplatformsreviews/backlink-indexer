from __future__ import annotations

import secrets
from typing import Iterable
from urllib.parse import urlparse

import httpx

from ..config import USER_AGENT

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
# Participating engines also publish their own IndexNow endpoints. Submitting
# to each one is still only an acceptance, not an index confirmation.
INDEXNOW_ENDPOINTS = {
    "indexnow": "https://api.indexnow.org/indexnow",
    "bing": "https://www.bing.com/indexnow",
    "yandex": "https://yandex.com/indexnow",
}


def generate_key() -> str:
    # IndexNow keys must be 8–128 hex or UUID-like characters.
    return secrets.token_hex(16)


def key_file_url(public_base_url: str, key: str) -> str:
    base = (public_base_url or "").rstrip("/")
    return f"{base}/{key}.txt" if base else f"/{key}.txt"


def hosts_for_urls(urls: Iterable[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            continue
        grouped.setdefault(host, []).append(url)
    return grouped


async def submit(
    client: httpx.AsyncClient,
    urls: list[str],
    key: str,
    key_location: str,
    endpoint: str = INDEXNOW_ENDPOINT,
) -> dict:
    """Submit a list of URLs that all share the host in key_location.

    IndexNow only accepts URLs whose host matches the key file host.
    Callers must group by host first.
    """
    if not urls:
        return {"ok": False, "status": None, "state": "rejected", "detail": "No URLs to submit.", "endpoint": endpoint}
    if not key:
        return {
            "ok": False,
            "status": None,
            "state": "rejected",
            "detail": "IndexNow key is not configured.",
            "endpoint": endpoint,
        }
    host = urlparse(urls[0]).hostname or urlparse(key_location).hostname or ""
    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": urls[:10000],
    }
    try:
        resp = await client.post(
            endpoint,
            json=payload,
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "detail": f"IndexNow request to {endpoint} failed: {exc}",
            "endpoint": endpoint,
            "state": "rejected",
        }
    # 200 or 202 are success; 422 usually means key/host mismatch.
    ok = resp.status_code in {200, 202}
    body = (resp.text or "")[:500]
    detail = f"HTTP {resp.status_code}"
    if body:
        detail = f"{detail}: {body}"
    if resp.status_code == 422:
        detail += (
            " — IndexNow rejected the payload. The key file must be publicly "
            "reachable on the same host as the submitted URLs."
        )
    return {
        "ok": ok,
        "status": resp.status_code,
        "detail": detail,
        "host": host,
        "count": len(urls),
        "endpoint": endpoint,
        "state": "accepted" if ok else "rejected",
    }


async def submit_all(
    client: httpx.AsyncClient,
    urls: list[str],
    key: str,
    key_location: str,
) -> dict[str, dict]:
    """POST the same IndexNow payload to the shared hub and to Bing and Yandex."""
    results = {}
    for name, endpoint in INDEXNOW_ENDPOINTS.items():
        results[name] = await submit(client, urls, key, key_location, endpoint=endpoint)
    return results
