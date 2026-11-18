"""Operator hub domains, hub pages, crawler hits, and crawl-time stats.

Hub domains are hostnames the operator points at this app. Each page lists at
most 20 external URLs. Sitemaps for a hub domain contain only that domain's
own URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from .database import get_conn, insert_id, tx, utcnow
from .services.crypto import EncryptionError, decrypt_text, encrypt_text
from .services.urlutil import is_safe_public_url

GOOGLE_DAILY = 200
INDEXNOW_DAILY = 10000
PAGE_URLS = 20
_HOST_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_DNS_CACHE: dict[str, tuple[float, bool]] = {}
_HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def clear_host_cache() -> None:
    _HOST_CACHE.clear()


def clean_hostname(value: str) -> str:
    text = (value or "").strip().lower()
    text = text.removeprefix("https://").removeprefix("http://")
    text = text.split("/")[0].split(":")[0].strip().rstrip(".")
    if text.startswith("www.") and text.count(".") == 1:
        return ""
    if not _HOST_RE.match(text):
        return ""
    if text in {"localhost", "testserver"} or text.endswith(".local"):
        return ""
    return text


def _row(row: Optional[dict]) -> Optional[dict]:
    return dict(row) if row else None


def list_domains() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM hub_domains ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def domain_by_id(domain_id: int) -> Optional[dict]:
    return _row(get_conn().execute("SELECT * FROM hub_domains WHERE id = ?", (domain_id,)).fetchone())


def domain_for_host(host: str) -> Optional[dict]:
    name = (host or "").split(",")[0].strip().lower().split(":")[0].rstrip(".")
    if not name:
        return None
    now = time.time()
    cached = _HOST_CACHE.get(name)
    if cached and now - cached[0] < 30:
        return cached[1]
    try:
        found = _row(get_conn().execute("SELECT * FROM hub_domains WHERE hostname = ?", (name,)).fetchone())
    except Exception:
        return None
    _HOST_CACHE[name] = (now, found)
    return found


def add_domain(hostname: str, google_push: bool) -> tuple[Optional[dict], str]:
    from .services.indexnow import generate_key

    name = clean_hostname(hostname)
    if not name:
        return None, "Enter a public hostname such as links.example.com."
    if domain_for_host(name):
        return None, "That hub domain is already registered."
    now_key = generate_key()
    with tx() as conn:
        domain_id = insert_id(
            conn,
            """INSERT INTO hub_domains
               (hostname, indexnow_key, indexnow_verified_at, google_push_enabled,
                gcp_projects_json_encrypted, daily_quota_used, last_push_at, last_error)
               VALUES (?, ?, '', ?, '', '', '', '')""",
            (name, now_key, 1 if google_push else 0),
        )
    clear_host_cache()
    return domain_by_id(domain_id), ""


def set_google_push(domain_id: int, enabled: bool) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE hub_domains SET google_push_enabled = ? WHERE id = ?",
            (1 if enabled else 0, domain_id),
        )
    clear_host_cache()


def delete_domain(domain_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM hub_domains WHERE id = ?", (domain_id,))
    clear_host_cache()


def gcp_projects(domain: dict) -> list[dict]:
    raw = domain.get("gcp_projects_json_encrypted") or ""
    if not raw:
        return []
    try:
        data = json.loads(decrypt_text(raw))
    except (EncryptionError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)]


def gcp_emails(domain: dict) -> list[str]:
    emails = []
    for info in gcp_projects(domain):
        email = (info.get("client_email") or "").strip()
        if email:
            emails.append(email)
    return emails


def add_gcp_project(domain_id: int, info: dict) -> str:
    if info.get("type") != "service_account" or not info.get("private_key") or not info.get("client_email"):
        return "Upload a Google service-account JSON key."
    domain = domain_by_id(domain_id)
    if not domain:
        return "Hub domain not found."
    projects = [item for item in gcp_projects(domain) if item.get("client_email") != info.get("client_email")]
    stored = {key: info.get(key) for key in info.keys() if not str(key).startswith("_")}
    projects.append(stored)
    try:
        token = encrypt_text(json.dumps(projects))
    except EncryptionError as exc:
        return str(exc)
    with tx() as conn:
        conn.execute(
            "UPDATE hub_domains SET gcp_projects_json_encrypted = ? WHERE id = ?",
            (token, domain_id),
        )
    clear_host_cache()
    return ""


def quota_snapshot(domain: dict) -> dict[str, Any]:
    today = utcnow()[:10]
    try:
        data = json.loads(domain.get("daily_quota_used") or "{}")
    except json.JSONDecodeError:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "indexnow": 0, "google": {}}
    google = data.get("google") if isinstance(data.get("google"), dict) else {}
    projects = gcp_projects(domain)
    per_project = []
    remaining_google = 0
    for index in range(len(projects)):
        used = int(google.get(str(index), 0) or 0)
        left = max(0, GOOGLE_DAILY - used)
        remaining_google += left
        per_project.append({"email": projects[index].get("client_email") or f"project {index + 1}", "used": used, "limit": GOOGLE_DAILY})
    indexnow_used = int(data.get("indexnow") or 0)
    return {
        "date": today,
        "indexnow_used": indexnow_used,
        "indexnow_limit": INDEXNOW_DAILY,
        "indexnow_remaining": max(0, INDEXNOW_DAILY - indexnow_used),
        "google_remaining": remaining_google,
        "google_projects": per_project,
        "raw": {"date": today, "indexnow": indexnow_used, "google": {str(k): int(v) for k, v in google.items()}},
    }


def _write_quota(domain_id: int, raw: dict) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE hub_domains SET daily_quota_used = ? WHERE id = ?",
            (json.dumps(raw), domain_id),
        )
    clear_host_cache()


def take_indexnow_slot(domain_id: int) -> bool:
    domain = domain_by_id(domain_id)
    if not domain:
        return False
    snap = quota_snapshot(domain)
    if snap["indexnow_remaining"] < 1:
        return False
    raw = snap["raw"]
    raw["indexnow"] = int(raw["indexnow"]) + 1
    _write_quota(domain_id, raw)
    return True


def take_google_slot(domain_id: int) -> Optional[tuple[int, dict]]:
    """Return (project index, service account) and consume one URL of that project's daily 200."""
    domain = domain_by_id(domain_id)
    if not domain:
        return None
    projects = gcp_projects(domain)
    if not projects:
        return None
    snap = quota_snapshot(domain)
    raw = snap["raw"]
    for index, info in enumerate(projects):
        used = int(raw["google"].get(str(index), 0) or 0)
        if used >= GOOGLE_DAILY:
            continue
        raw["google"][str(index)] = used + 1
        _write_quota(domain_id, raw)
        return index, info
    return None


def reset_quotas_if_needed() -> int:
    """Clear yesterday's counters. Called by the daily cron and before a push."""
    changed = 0
    today = utcnow()[:10]
    for domain in list_domains():
        try:
            data = json.loads(domain.get("daily_quota_used") or "{}")
        except json.JSONDecodeError:
            data = {}
        if data.get("date") == today:
            continue
        _write_quota(domain["id"], {"date": today, "indexnow": 0, "google": {}})
        changed += 1
    return changed


def mark_domain_result(domain_id: int, *, ok: bool, detail: str) -> None:
    now = utcnow()
    with tx() as conn:
        if ok:
            conn.execute(
                "UPDATE hub_domains SET last_push_at = ?, last_error = '' WHERE id = ?",
                (now, domain_id),
            )
        else:
            conn.execute(
                "UPDATE hub_domains SET last_error = ? WHERE id = ?",
                (detail[:500], domain_id),
            )
    clear_host_cache()


async def verify_indexnow(client, domain: dict) -> dict[str, Any]:
    key = domain.get("indexnow_key") or ""
    url = f"https://{domain['hostname']}/{key}.txt"
    safe, reason = is_safe_public_url(url)
    if not safe:
        return {"ok": False, "detail": reason}
    try:
        response = await client.get(url, timeout=15, follow_redirects=True)
    except Exception as exc:
        return {"ok": False, "detail": f"Could not reach the key file: {exc}"}
    body = (response.text or "").strip()
    if response.status_code == 200 and body == key:
        with tx() as conn:
            conn.execute(
                "UPDATE hub_domains SET indexnow_verified_at = ? WHERE id = ?",
                (utcnow(), domain["id"]),
            )
        clear_host_cache()
        return {"ok": True, "detail": "IndexNow key file matches."}
    return {
        "ok": False,
        "detail": f"HTTP {response.status_code}. The file must contain only the key.",
    }


def intro_text(slug: str, hostname: str, count: int) -> str:
    """Short unique copy. The sentences are fixed templates, not spun text."""
    seed = int(hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8], 16)
    host = hostname or "this site"
    openings = (
        f"This page gathers {count} web addresses for visitors of {host}.",
        f"{host} is publishing {count} links that were submitted for discovery.",
        f"A short list of {count} pages is collected here on {host}.",
    )
    middles = (
        "Each address points at someone else's site, so this list can only help a crawler find it.",
        "The pages below are third-party URLs. Publishing them here does not submit them as if we owned them.",
        "These addresses were accepted after a pre-check and placed on this crawl page.",
    )
    closes = (
        "Search engines choose what to keep. A listing is not an index confirmation.",
        "Whether a page is indexed is checked later in live search results, not from this listing.",
        "A report counts a URL as indexed only after a search result confirms it.",
    )
    return " ".join((openings[seed % 3], middles[(seed // 3) % 3], closes[(seed // 9) % 3]))


def _slug_taken(domain_id: Optional[int], slug: str) -> bool:
    """True when this slug is already used on the same hub domain.

    A fallback page has no hub domain. Postgres cannot infer a type for a
    parameter that appears only in `IS NULL`, so that case must not bind NULL.
    """
    if domain_id is None:
        row = get_conn().execute(
            "SELECT id FROM hub_pages WHERE slug = ? AND hub_domain_id IS NULL",
            (slug,),
        ).fetchone()
    else:
        row = get_conn().execute(
            "SELECT id FROM hub_pages WHERE slug = ? AND hub_domain_id = ?",
            (slug, int(domain_id)),
        ).fetchone()
    return row is not None


def _unique_slug(domain_id: Optional[int]) -> str:
    for _ in range(8):
        slug = secrets.token_hex(4)
        if not _slug_taken(domain_id, slug):
            return slug
    slug = secrets.token_hex(8)
    if not _slug_taken(domain_id, slug):
        return slug
    return secrets.token_hex(8)


def create_page(domain: Optional[dict], url_ids: list[int]) -> dict:
    domain_id = int(domain["id"]) if domain else None
    slug = _unique_slug(domain_id)
    hostname = domain["hostname"] if domain else ""
    intro = intro_text(slug, hostname, len(url_ids))
    now = utcnow()
    with tx() as conn:
        page_id = insert_id(
            conn,
            """INSERT INTO hub_pages
               (hub_domain_id, slug, url_ids_json, intro_text, pushes_count, last_pushed_at, bot_hits_json, created_at)
               VALUES (?, ?, ?, ?, 0, '', '{}', ?)""",
            (domain_id, slug, json.dumps(url_ids), intro, now),
        )
        for url_id in url_ids:
            conn.execute(
                """UPDATE urls
                   SET hub_page_id = ?, status = 'submitted',
                       submitted_at = CASE WHEN submitted_at IS NULL OR submitted_at = '' THEN ? ELSE submitted_at END,
                       updated_at = ?
                   WHERE id = ? AND status IN ('queued', '')""",
                (page_id, now, now, url_id),
            )
    page = page_by_id(page_id)
    return page or {"id": page_id, "slug": slug, "hub_domain_id": domain_id, "intro_text": intro, "url_ids_json": json.dumps(url_ids)}


def page_by_id(page_id: int) -> Optional[dict]:
    return _row(get_conn().execute("SELECT * FROM hub_pages WHERE id = ?", (page_id,)).fetchone())


def page_by_slug(domain_id: Optional[int], slug: str) -> Optional[dict]:
    if domain_id is None:
        return _row(
            get_conn().execute(
                "SELECT * FROM hub_pages WHERE hub_domain_id IS NULL AND slug = ?",
                (slug,),
            ).fetchone()
        )
    return _row(
        get_conn().execute(
            "SELECT * FROM hub_pages WHERE hub_domain_id = ? AND slug = ?",
            (domain_id, slug),
        ).fetchone()
    )


def pages_for_domain(domain_id: Optional[int]) -> list[dict]:
    if domain_id is None:
        rows = get_conn().execute(
            "SELECT * FROM hub_pages WHERE hub_domain_id IS NULL ORDER BY id"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM hub_pages WHERE hub_domain_id = ? ORDER BY id",
            (domain_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def page_http_url(domain: Optional[dict], slug: str, public_base: str) -> str:
    if domain and domain.get("hostname"):
        return f"https://{domain['hostname']}/l/{slug}"
    base = (public_base or "").rstrip("/")
    return f"{base}/l/{slug}" if base else f"/l/{slug}"


def neighbor_urls(domain: Optional[dict], page: dict, public_base: str) -> tuple[str, str]:
    pages = pages_for_domain(page.get("hub_domain_id"))
    ids = [item["id"] for item in pages]
    if page["id"] not in ids:
        return "", ""
    index = ids.index(page["id"])
    prev_url = page_http_url(domain, pages[index - 1]["slug"], public_base) if index > 0 else ""
    next_url = page_http_url(domain, pages[index + 1]["slug"], public_base) if index + 1 < len(pages) else ""
    return prev_url, next_url


def urls_on_page(page: dict) -> list[dict]:
    try:
        ids = json.loads(page.get("url_ids_json") or "[]")
    except json.JSONDecodeError:
        ids = []
    ids = [int(item) for item in ids]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = get_conn().execute(
        f"SELECT * FROM urls WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    order = {url_id: index for index, url_id in enumerate(ids)}
    found = [dict(row) for row in rows]
    found.sort(key=lambda item: order.get(int(item["id"]), 0))
    return found


def remember_successful_push(page_id: int) -> None:
    now = utcnow()
    with tx() as conn:
        row = conn.execute("SELECT bot_hits_json, pushes_count FROM hub_pages WHERE id = ?", (page_id,)).fetchone()
        if not row:
            return
        try:
            data = json.loads(row["bot_hits_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if not data.get("first_push_at"):
            data["first_push_at"] = now
        conn.execute(
            """UPDATE hub_pages
               SET pushes_count = ?, last_pushed_at = ?, bot_hits_json = ?
               WHERE id = ?""",
            (int(row["pushes_count"] or 0) + 1, now, json.dumps(data), page_id),
        )


def log_event(url_id: Optional[int], event_type: str, payload: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO events (url_id, type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (url_id, event_type, json.dumps(payload, default=str)[:4000], utcnow()),
        )


def recent_events(limit: int = 40) -> list[dict]:
    rows = get_conn().execute(
        """SELECT id, url_id, type, payload_json, created_at FROM events
           WHERE type IN ('hub_push', 'indexnow_push', 'google_push', 'credit_refund', 'serp_check', 'precheck')
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        payload.pop("response_text", None)
        item["payload"] = {key: value for key, value in payload.items() if "private_key" not in key and "token" not in key}
        out.append(item)
    return out


def bot_family(user_agent: str) -> str:
    text = (user_agent or "").lower()
    if "googlebot" in text:
        return "google"
    if "bingbot" in text:
        return "bing"
    if "yandexbot" in text or "yandex.com/bots" in text:
        return "yandex"
    return ""


def _forward_matches(hostname: str, ip: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    found = {item[4][0] for item in infos}
    return ip in found


def verify_crawler(ip: str, family: str) -> bool:
    """Reverse-DNS check. Googlebot must resolve under googlebot.com or google.com and forward-confirm."""
    ip = (ip or "").strip()
    if not ip or family not in {"google", "bing", "yandex"}:
        return False
    cache_key = f"{family}|{ip}"
    cached = _DNS_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    ok = False
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(2.0)
    try:
        hostname = socket.gethostbyaddr(ip)[0].lower().rstrip(".")
        if family == "google":
            ok = hostname.endswith(".googlebot.com") or hostname.endswith(".google.com")
        elif family == "bing":
            ok = hostname.endswith(".search.msn.com")
        elif family == "yandex":
            ok = hostname.endswith(".yandex.ru") or hostname.endswith(".yandex.net") or hostname.endswith(".yandex.com")
        if ok:
            ok = _forward_matches(hostname, ip)
    except (socket.herror, socket.gaierror, OSError):
        ok = False
    finally:
        socket.setdefaulttimeout(previous_timeout)
    _DNS_CACHE[cache_key] = (time.time(), ok)
    return ok


def record_bot_hit(page_id: int, user_agent: str, ip: str) -> None:
    family = bot_family(user_agent)
    if not family:
        return
    verified = verify_crawler(ip, family)
    if not verified:
        log_event(None, "bot_unverified", {"page_id": page_id, "family": family})
        return
    now = utcnow()
    url_ids: list[int] = []
    with tx() as conn:
        row = conn.execute("SELECT bot_hits_json, url_ids_json FROM hub_pages WHERE id = ?", (page_id,)).fetchone()
        if not row:
            return
        try:
            data = json.loads(row["bot_hits_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        slot = data.get(family) if isinstance(data.get(family), dict) else {}
        slot["count"] = int(slot.get("count") or 0) + 1
        slot["verified"] = True
        if not slot.get("first_at"):
            slot["first_at"] = now
        data[family] = slot
        conn.execute("UPDATE hub_pages SET bot_hits_json = ? WHERE id = ?", (json.dumps(data), page_id))
        try:
            url_ids = [int(item) for item in json.loads(row["url_ids_json"] or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError):
            url_ids = []
        if url_ids:
            placeholders = ",".join("?" * len(url_ids))
            conn.execute(
                f"""UPDATE urls SET status = 'discovered', updated_at = ?
                    WHERE id IN ({placeholders}) AND status = 'submitted'""",
                [now, *url_ids],
            )
    log_event(url_ids[0] if url_ids else None, "bot_hit", {"page_id": page_id, "family": family, "verified": True})


def _parse(stamp: str) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    whole = int(round(seconds))
    if whole < 90:
        return f"{whole} seconds"
    minutes, leftover = divmod(whole, 60)
    if minutes < 90:
        if leftover >= 30:
            minutes += 1
        return f"{minutes} minutes"
    hours, minutes = divmod(minutes, 60)
    if minutes:
        return f"{hours} hours {minutes} minutes"
    return f"{hours} hours"


def googlebot_stats(project_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Measured push-to-first-verified-Googlebot times for visits whose first hit is today.

    Returns None when nothing was measured. Callers must not invent zeros.
    """
    today = utcnow()[:10]
    pages = get_conn().execute("SELECT * FROM hub_pages").fetchall()
    deltas: list[float] = []
    tracked: set[int] = set()
    for page in pages:
        try:
            data = json.loads(page["bot_hits_json"] or "{}")
            ids = [int(item) for item in json.loads(page["url_ids_json"] or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if project_id is not None and ids:
            placeholders = ",".join("?" * len(ids))
            rows = get_conn().execute(
                f"SELECT id FROM urls WHERE project_id = ? AND id IN ({placeholders})",
                [project_id, *ids],
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
        elif project_id is not None:
            ids = []
        for url_id in ids:
            tracked.add(url_id)
        google = data.get("google") if isinstance(data.get("google"), dict) else {}
        first = str(google.get("first_at") or "")
        pushed = str(data.get("first_push_at") or "")
        if not ids or not google.get("verified") or not first.startswith(today) or not pushed:
            continue
        start = _parse(pushed)
        end = _parse(first)
        if not start or not end:
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            continue
        deltas.extend([delta] * len(ids))
    if not deltas:
        return None
    return {
        "avg_seconds": round(sum(deltas) / len(deltas), 1),
        "min_seconds": round(min(deltas), 1),
        "max_seconds": round(max(deltas), 1),
        "avg_label": format_duration(sum(deltas) / len(deltas)),
        "min_label": format_duration(min(deltas)),
        "max_label": format_duration(max(deltas)),
        "tracked": len(tracked),
        "visits": len(deltas),
    }


def client_ip(headers: dict, fallback: str = "") -> str:
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return fallback or ""


def domain_sitemap_locs(domain: dict) -> list[tuple[str, str]]:
    base = f"https://{domain['hostname']}"
    locs = [(base + "/", utcnow())]
    for page in pages_for_domain(domain["id"]):
        stamp = page.get("last_pushed_at") or page.get("created_at") or utcnow()
        locs.append((f"{base}/l/{page['slug']}", stamp))
    return locs


def fallback_page_locs(public_base: str) -> list[tuple[str, str]]:
    base = (public_base or "").rstrip("/")
    locs = []
    for page in pages_for_domain(None):
        stamp = page.get("last_pushed_at") or page.get("created_at") or utcnow()
        locs.append((f"{base}/l/{page['slug']}", stamp))
    return locs
