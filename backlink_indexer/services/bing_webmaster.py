"""Bing Webmaster URL Submission API.

SubmitUrlBatch: https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlBatch
Documented limit: up to 10,000 URLs per day for a site verified in Bing Webmaster
Tools. A 200 response means Bing accepted the submission. It does not mean the
URL is indexed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from ..config import USER_AGENT
from ..database import get_setting, set_setting, tx, utcnow

SUBMIT_URL = "https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlBatch"
SITES_URL = "https://ssl.bing.com/webmaster/api.svc/json/GetUserSites"
QUOTA_URL = "https://ssl.bing.com/webmaster/api.svc/json/GetUrlSubmissionQuota"
DAILY_LIMIT = 10000
CACHE_SECONDS = 3600
BATCH_CHUNK = 500


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


def _fresh(stamp: str) -> bool:
    try:
        moment = datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() < CACHE_SECONDS


def _site_parts(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw:
        return "", "/"
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and not path.endswith("/"):
        path += "/"
    return host, path


def match_site(url: str, sites: list[str]) -> str | None:
    """Pick the verified Bing property for this URL.

    Hosts match with or without www. A path-prefix property (github.io, wix)
    matches only when the link path starts with that prefix. The longest
    prefix wins.
    """
    host, _path = _site_parts(url)
    if not host:
        return None
    link_path = urlparse(url if "://" in (url or "") else "https://" + (url or "")).path or "/"
    if not link_path.startswith("/"):
        link_path = "/" + link_path
    best = None
    best_len = -1
    for site in sites or []:
        site_host, site_path = _site_parts(site)
        if not site_host or site_host != host:
            continue
        if site_path == "/":
            length = 0
        else:
            comparable = link_path if link_path.endswith("/") else link_path + "/"
            if not (comparable.startswith(site_path) or link_path.rstrip("/") == site_path.rstrip("/")):
                continue
            length = len(site_path)
        if length > best_len:
            best = site
            best_len = length
    return best


def cached_sites() -> list[str] | None:
    stamp = get_setting("bing_sites_at") or ""
    raw = get_setting("bing_sites_json") or ""
    if not raw or not _fresh(stamp):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [str(item) for item in data if str(item).strip()]


def _store_sites(sites: list[str]) -> None:
    set_setting("bing_sites_json", json.dumps(sites))
    set_setting("bing_sites_at", utcnow())


def _quota_cache() -> dict:
    raw = get_setting("bing_quota_json") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _quota_cached(site: str) -> int | None:
    entry = _quota_cache().get(site) or {}
    if not isinstance(entry, dict) or not _fresh(str(entry.get("at") or "")):
        return None
    try:
        return max(0, int(entry.get("daily")))
    except (TypeError, ValueError):
        return None


def _store_quota(site: str, daily: int) -> None:
    data = _quota_cache()
    data[site] = {"daily": max(0, int(daily)), "at": utcnow()}
    set_setting("bing_quota_json", json.dumps(data))


def _lower_quota(site: str, used: int) -> None:
    current = _quota_cached(site)
    if current is None:
        return
    _store_quota(site, max(0, current - used))


def _unwrap(payload):
    if isinstance(payload, dict) and "d" in payload:
        return payload["d"]
    return payload


def _parse_sites(payload) -> list[str]:
    items = _unwrap(payload)
    if isinstance(items, dict):
        items = [items]
    sites: list[str] = []
    for item in items or []:
        if isinstance(item, str) and item.strip():
            sites.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        if item.get("IsVerified") is False:
            continue
        site = (item.get("SiteUrl") or item.get("Url") or "").strip()
        if site:
            sites.append(site)
    return sites


async def get_user_sites(client: httpx.AsyncClient, api_key: str) -> list[str]:
    """Verified Bing sites. Cached for about an hour so serverless instances reuse it."""
    cached = cached_sites()
    if cached is not None:
        return cached
    response = await client.get(
        SITES_URL,
        params={"apikey": api_key},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    sites = _parse_sites(response.json())
    _store_sites(sites)
    return sites


async def site_quota(client: httpx.AsyncClient, api_key: str, site_url: str) -> int | None:
    """Remaining submissions today for one site. None when the quota call fails."""
    cached = _quota_cached(site_url)
    if cached is not None:
        return cached
    try:
        response = await client.get(
            QUOTA_URL,
            params={"apikey": api_key, "siteUrl": site_url},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        body = _unwrap(response.json())
        if not isinstance(body, dict):
            return None
        daily = body.get("DailyQuota")
        if daily is None:
            return None
        remaining = max(0, int(daily))
    except Exception:
        return None
    _store_quota(site_url, remaining)
    return remaining


async def submit_batch(
    client: httpx.AsyncClient,
    api_key: str,
    site_url: str,
    urls: list[str],
) -> dict[str, dict]:
    """SubmitUrlBatch for one verified site. A 200 is accepted, not indexed."""
    results: dict[str, dict] = {}
    for start in range(0, len(urls), BATCH_CHUNK):
        chunk = urls[start:start + BATCH_CHUNK]
        try:
            response = await client.post(
                SUBMIT_URL,
                params={"apikey": api_key},
                json={"siteUrl": site_url, "urlList": chunk},
                timeout=30,
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"},
            )
        except Exception as exc:
            for url in chunk:
                results[url] = {
                    "ok": False,
                    "state": "rejected",
                    "site": site_url,
                    "detail": f"Bing site {site_url}. Bing Webmaster request failed: {exc}",
                }
            continue
        body = (response.text or "")[:300]
        if response.status_code == 200:
            _consume(len(chunk))
            _lower_quota(site_url, len(chunk))
            detail = (
                f"Bing site {site_url}. HTTP {response.status_code}. "
                "Accepted for submission. This is not an index confirmation."
            )
            for url in chunk:
                results[url] = {"ok": True, "state": "accepted", "site": site_url, "detail": detail}
        else:
            detail = f"Bing site {site_url}. HTTP {response.status_code}"
            if body:
                detail = f"{detail}: {body}"
            for url in chunk:
                results[url] = {"ok": False, "state": "rejected", "site": site_url, "detail": detail}
    return results


async def plan_submissions(
    client: httpx.AsyncClient,
    api_key: str,
    urls: list[str],
    sites: list[str],
) -> dict[str, dict]:
    """Group URLs by matched Bing site and submit one batch per site."""
    results: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    for url in urls:
        site = match_site(url, sites)
        if not site:
            results[url] = {
                "ok": False,
                "state": "not_verified",
                "site": "",
                "detail": "Not verified in Bing. IndexNow and the crawl hub still apply.",
            }
            continue
        groups.setdefault(site, []).append(url)
    for site, group in groups.items():
        remaining = await site_quota(client, api_key, site)
        if remaining is None:
            remaining = quota_remaining()
        take = group[:remaining]
        for url in group[remaining:]:
            results[url] = {
                "ok": False,
                "state": "deferred",
                "site": site,
                "detail": (
                    f"Bing site {site}. Daily submission quota is used. "
                    "This URL stays queued and will be submitted on a later day."
                ),
            }
        if take:
            results.update(await submit_batch(client, api_key, site, take))
    return results
