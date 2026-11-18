"""Search Console OAuth, IndexNow-per-domain, sitemaps, and URL Inspection.

Inspection can move a URL to discovered. It never marks a URL indexed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx

from ..database import get_conn, tx, utcnow
from .crypto import EncryptionError, decrypt_text, encrypt_text
from .urlutil import host_of, registrable_host

WEBMASTERS_SCOPE = "https://www.googleapis.com/auth/webmasters"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


def _secret() -> bytes:
    return (os.environ.get("ENCRYPTION_KEY") or "").encode("utf-8")


def load_config(user_id: int, provider: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT config_encrypted, enabled FROM connections WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    ).fetchone()
    if not row or not row.get("config_encrypted"):
        return {}
    if str(row.get("enabled")) in {"0", "False"}:
        return {}
    try:
        data = json.loads(decrypt_text(row["config_encrypted"]))
    except (EncryptionError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(user_id: int, provider: str, config: dict[str, Any], enabled: bool = True) -> None:
    token = encrypt_text(json.dumps(config))
    with tx() as conn:
        conn.execute(
            """INSERT INTO connections (user_id, provider, config_encrypted, enabled)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (user_id, provider)
               DO UPDATE SET config_encrypted = excluded.config_encrypted, enabled = excluded.enabled""",
            (user_id, provider, token, 1 if enabled else 0),
        )


def verified_properties(user_id: int) -> list[str]:
    config = load_config(user_id, "gsc")
    values = config.get("properties") or []
    return [str(item) for item in values if str(item).strip()]


def property_matches(host: str, properties: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for prop in properties:
        prop = (prop or "").strip()
        if prop.lower().startswith("sc-domain:"):
            domain = prop.split(":", 1)[1].lower().strip().rstrip(".")
            if host == domain or host.endswith("." + domain):
                return True
            continue
        parsed = urlparse(prop if "://" in prop else "https://" + prop)
        prop_host = (parsed.hostname or "").lower()
        if prop_host and prop_host == host:
            return True
    return False


def classify_kind(user_id: int, url: str) -> str:
    try:
        properties = verified_properties(user_id)
    except Exception:
        return "external"
    if property_matches(host_of(url), properties):
        return "own"
    return "external"


def make_oauth_state(user_id: int) -> str:
    secret = _secret()
    if not secret:
        return ""
    stamp = int(time.time())
    msg = f"{int(user_id)}.{stamp}"
    sig = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def read_oauth_state(state: str) -> Optional[int]:
    secret = _secret()
    parts = (state or "").split(".")
    if len(parts) != 3 or not secret:
        return None
    user_s, stamp_s, sig = parts
    msg = f"{user_s}.{stamp_s}"
    expected = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        stamp = int(stamp_s)
        user_id = int(user_s)
    except ValueError:
        return None
    if abs(int(time.time()) - stamp) > 900:
        return None
    return user_id


def oauth_start_url(user_id: int, redirect_uri: str) -> str:
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    if not client_id or not _secret():
        return ""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": WEBMASTERS_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": make_oauth_state(user_id),
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urlencode(params)


async def exchange_code(client: httpx.AsyncClient, code: str, redirect_uri: str) -> dict[str, Any]:
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return {"ok": False, "detail": "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set."}
    response = await client.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        return {"ok": False, "detail": f"HTTP {response.status_code}: {response.text[:300]}"}
    payload = response.json()
    return {"ok": True, "token": payload}


async def _refresh(client: httpx.AsyncClient, config: dict[str, Any]) -> dict[str, Any]:
    expires = int(config.get("expires_at") or 0)
    if config.get("access_token") and expires > int(time.time()) + 60:
        return config
    refresh = config.get("refresh_token") or ""
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not refresh or not client_id or not client_secret:
        return config
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        return config
    payload = response.json()
    config["access_token"] = payload.get("access_token") or config.get("access_token")
    config["expires_at"] = int(time.time()) + int(payload.get("expires_in") or 3600)
    return config


async def authorized_config(client: httpx.AsyncClient, user_id: int) -> dict[str, Any]:
    config = load_config(user_id, "gsc")
    if not config:
        return {}
    refreshed = await _refresh(client, dict(config))
    if refreshed.get("access_token") != config.get("access_token"):
        save_config(user_id, "gsc", refreshed)
    return refreshed


def _auth_headers(config: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.get('access_token')}", "Accept": "application/json"}


async def fetch_properties(client: httpx.AsyncClient, config: dict[str, Any]) -> list[str]:
    if not config.get("access_token"):
        return []
    response = await client.get(SITES_URL, headers=_auth_headers(config), timeout=30)
    if response.status_code >= 400:
        return []
    payload = response.json()
    found = []
    for entry in payload.get("siteEntry") or []:
        url = (entry.get("siteUrl") or "").strip()
        permission = (entry.get("permissionLevel") or "").lower()
        if url and permission and "unverified" not in permission:
            found.append(url)
        elif url and not permission:
            found.append(url)
    return found


async def submit_sitemap(
    client: httpx.AsyncClient, config: dict[str, Any], site_url: str, feed_url: str
) -> dict[str, Any]:
    if not config.get("access_token"):
        return {"ok": False, "detail": "Search Console is not connected."}
    endpoint = (
        "https://www.googleapis.com/webmasters/v3/sites/"
        + quote(site_url, safe="")
        + "/sitemaps/"
        + quote(feed_url, safe="")
    )
    response = await client.put(endpoint, headers=_auth_headers(config), timeout=30)
    ok = response.status_code in {200, 204}
    return {"ok": ok, "status": response.status_code, "detail": f"HTTP {response.status_code}: {(response.text or '')[:300]}"}


async def inspect_url(
    client: httpx.AsyncClient, config: dict[str, Any], site_url: str, page_url: str
) -> dict[str, Any]:
    if not config.get("access_token"):
        return {"ok": False, "crawled": False, "detail": "Search Console is not connected."}
    response = await client.post(
        INSPECT_URL,
        headers={**_auth_headers(config), "Content-Type": "application/json"},
        json={"inspectionUrl": page_url, "siteUrl": site_url},
        timeout=30,
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "crawled": False,
            "status": response.status_code,
            "detail": f"HTTP {response.status_code}: {(response.text or '')[:400]}",
        }
    payload = response.json()
    index_status = ((payload.get("inspectionResult") or {}).get("indexStatusResult") or {})
    coverage = str(index_status.get("coverageState") or "")
    last_crawl = str(index_status.get("lastCrawlTime") or "")
    crawled = bool(last_crawl) or "crawl" in coverage.lower() or "indexed" in coverage.lower()
    return {
        "ok": True,
        "crawled": crawled,
        "coverage": coverage,
        "last_crawl": last_crawl,
        "detail": coverage or "Inspection returned no coverage state.",
        "status": response.status_code,
    }


def inspection_href(property_url: str, page_url: str) -> str:
    if not property_url or not page_url:
        return ""
    return (
        "https://search.google.com/search-console/inspect?resource_id="
        + quote(property_url, safe="")
        + "&id="
        + quote(page_url, safe="")
    )


def matching_property(host: str, properties: list[str]) -> str:
    host = (host or "").lower()
    for prop in properties:
        if property_matches(host, [prop]):
            return prop
    return ""


def indexnow_hosts(user_id: int) -> dict[str, Any]:
    config = load_config(user_id, "indexnow")
    hosts = config.get("hosts") or {}
    return hosts if isinstance(hosts, dict) else {}


def indexnow_for(user_id: int, host: str) -> dict[str, Any]:
    raw = (host or "").strip().lower()
    if "://" in raw:
        raw = host_of(raw)
    hosts = indexnow_hosts(user_id)
    if raw in hosts:
        return hosts[raw]
    bare = registrable_host(raw)
    return hosts.get(bare) or {}


def save_indexnow_host(user_id: int, host: str, key: str, verified_at: str = "") -> None:
    host = (host or "").lower().strip().rstrip(".")
    config = load_config(user_id, "indexnow")
    hosts = config.get("hosts") if isinstance(config.get("hosts"), dict) else {}
    current = hosts.get(host) or {}
    current["key"] = key
    if verified_at:
        current["verified_at"] = verified_at
    elif "verified_at" not in current:
        current["verified_at"] = ""
    hosts[host] = current
    config["hosts"] = hosts
    save_config(user_id, "indexnow", config)


def inspect_quota_take(user_id: int, property_url: str, limit: int = 2000) -> bool:
    """Reserve one URL Inspection call for this property today."""
    from ..database import tx as _tx

    day = utcnow()[:10]
    digest = hashlib.sha256(f"{user_id}|{property_url}".encode("utf-8")).hexdigest()[:16]
    date_key = f"inspect_date_{digest}"
    count_key = f"inspect_count_{digest}"
    with _tx() as conn:
        row_date = conn.execute("SELECT value FROM settings WHERE key = ?", (date_key,)).fetchone()
        row_count = conn.execute("SELECT value FROM settings WHERE key = ?", (count_key,)).fetchone()
        current = 0
        if row_date and row_date["value"] == day and row_count and row_count["value"]:
            try:
                current = int(row_count["value"])
            except ValueError:
                current = 0
        if current >= limit:
            return False
        for key, value in ((date_key, day), (count_key, str(current + 1))):
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
    return True


def store_oauth(user_id: int, token: dict[str, Any], properties: list[str]) -> None:
    previous = load_config(user_id, "gsc")
    refresh = token.get("refresh_token") or previous.get("refresh_token") or ""
    config = {
        "access_token": token.get("access_token") or "",
        "refresh_token": refresh,
        "expires_at": int(time.time()) + int(token.get("expires_in") or 3600),
        "properties": properties,
    }
    save_config(user_id, "gsc", config)
