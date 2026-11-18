"""Pre-check, drip onto hub pages, SERP confirmation, and refunds.

Push success never sets indexed. Reports count confirmed indexed URLs only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from . import network
from .network import googlebot_stats as googlebot_stats
from .credits import confirm_indexed, refund_expired, settle, summary_for_project
from .database import get_conn, get_setting, set_setting, tx, utcnow
from .limits import is_unlimited_email, normalize_email, operator_email_for_username, release_added
from .services import gsc, google_indexing, indexnow, serp
from .services.precheck import precheck_url
from .services.quotas import consume as consume_global_quota
from .services.quotas import remaining as global_quota_remaining
from .services.secrets import setting_or_env
from .services.urlutil import canonicalize, host_of

logger = logging.getLogger(__name__)

CHECK_DAYS = (1, 3, 7, 14, 28)
GOOGLE_PUSH_WARNING = (
    "Google officially permits the Indexing API only for JobPosting and BroadcastEvent pages. "
    "Other use can get the project's quota revoked. Use at your own risk."
)


def live_precheck_enabled() -> bool:
    return (os.environ.get("INDEXER_SKIP_LIVE_PRECHECK") or "").strip().lower() not in {"1", "true", "yes"}


def _host_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("INDEXER_HOST_INTERVAL", "1")))
    except ValueError:
        return 1.0


def _parse(stamp: str) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class PacedClient:
    """At most one request per second per host, including robots.txt."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._last: dict[str, float] = {}

    async def _pace(self, url: str) -> None:
        host = (urlparse(str(url)).hostname or "").lower()
        interval = _host_interval()
        if not host or interval <= 0:
            return
        wait = interval - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            import asyncio

            await asyncio.sleep(wait)
        self._last[host] = time.monotonic()

    async def get(self, url: str, **kwargs):
        await self._pace(url)
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self._client.post(url, **kwargs)


def _project_user(url_id: int) -> Optional[int]:
    row = get_conn().execute(
        "SELECT p.user_id FROM urls u JOIN projects p ON p.id = u.project_id WHERE u.id = ?",
        (url_id,),
    ).fetchone()
    return int(row["user_id"]) if row else None


def _definitive_reason(result: dict) -> str:
    error = (result.get("fetch_error") or "").lower()
    if result.get("http_status") is None and error:
        if any(word in error for word in ("blocked", "private", "local", "metadata", "only http")):
            return result.get("fetch_error") or "URL is not sendable."
        return ""
    if result.get("robots_disallow"):
        return "robots.txt disallows this URL."
    status = result.get("http_status")
    if status is not None and int(status) >= 400:
        return f"HTTP {int(status)}"
    if result.get("noindex"):
        return "noindex"
    return ""


def _error_count(url_id: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM events WHERE url_id = ? AND type = 'precheck_error'",
        (url_id,),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _note_failure(errors: list[dict], exc: Exception, **fields: Any) -> None:
    """Record one item failure and clear an aborted Postgres transaction."""
    try:
        get_conn().rollback()
    except Exception:
        pass
    logger.exception("cron item failed")
    message = f"{type(exc).__name__}: {exc}"[:500]
    errors.append({**fields, "error": message})


async def precheck_ids(url_ids: list[int], client: Optional[httpx.AsyncClient] = None) -> dict[str, list]:
    accepted: list[dict] = []
    not_sendable: list[dict] = []
    skipped: list[str] = []
    errors: list[dict] = []
    if not url_ids:
        return {"accepted": accepted, "not_sendable": not_sendable, "skipped_duplicates": skipped, "errors": errors}
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=4)
    paced = PacedClient(client)
    started = time.monotonic()
    try:
        budget = float(os.environ.get("PRECHECK_BUDGET_SECONDS", "8"))
    except ValueError:
        budget = 8.0
    budget = max(3.0, min(9.0, budget))
    try:
        placeholders = ",".join("?" * len(url_ids))
        rows = get_conn().execute(
            f"""SELECT u.*, p.target_domain, p.include_subdomains, p.user_id
                FROM urls u JOIN projects p ON p.id = u.project_id
                WHERE u.id IN ({placeholders}) ORDER BY u.id""",
            url_ids,
        ).fetchall()
        for row in rows:
            if time.monotonic() - started > budget:
                accepted.append({"id": int(row["id"]), "url": row["url"], "status": "queued", "kind": row.get("kind") or "external"})
                continue
            try:
                result = await precheck_url(
                    paced,
                    row["url"],
                    (row.get("target_domain") or host_of(row["url"]) or "example.com"),
                    bool(row.get("include_subdomains", 1)),
                )
                outcome = _apply_precheck(dict(row), result)
            except Exception as exc:
                _note_failure(errors, exc, url_id=int(row["id"]))
                continue
            if outcome["bucket"] == "accepted":
                accepted.append(outcome["item"])
            elif outcome["bucket"] == "not_sendable":
                not_sendable.append(outcome["item"])
            elif outcome["bucket"] == "duplicate":
                skipped.append(outcome["item"]["url"])
            else:
                accepted.append({"id": int(row["id"]), "url": row["url"], "status": "queued", "kind": row.get("kind") or "external"})
    finally:
        if owns_client:
            await client.aclose()
    return {"accepted": accepted, "not_sendable": not_sendable, "skipped_duplicates": skipped, "errors": errors}


def _apply_precheck(row: dict, result: dict) -> dict[str, Any]:
    url_id = int(row["id"])
    reason = _definitive_reason(result)
    now = utcnow()
    if not reason and result.get("http_status") is None:
        attempts = _error_count(url_id) + 1
        detail = result.get("fetch_error") or "Pre-check did not finish."
        network.log_event(url_id, "precheck_error", {"detail": detail, "attempt": attempts})
        if attempts >= 3:
            reason = f"Pre-check failed: {detail}"
        else:
            return {"bucket": "retry", "item": {}}
    final = canonicalize(result.get("final_url") or row["url"]) or row["url"]
    if not reason:
        clash = get_conn().execute(
            """SELECT id FROM urls
               WHERE project_id = ? AND id != ? AND (normalized_url = ? OR url = ?)""",
            (row["project_id"], url_id, final, final),
        ).fetchone()
        if clash:
            user_id = row.get("user_id") or _project_user(url_id)
            settle(url_id, "released", "Duplicate of the final URL after redirects.")
            with tx() as conn:
                conn.execute("DELETE FROM urls WHERE id = ?", (url_id,))
            if user_id:
                release_added(int(user_id), 1)
            return {"bucket": "duplicate", "item": {"url": row["url"]}}
    kind = gsc.classify_kind(int(row["user_id"]), final if not reason else row["url"])
    payload = {
        "http_status": result.get("http_status"),
        "final_url": result.get("final_url"),
        "noindex": bool(result.get("noindex")),
        "canonical": result.get("canonical"),
        "robots_disallow": result.get("robots_disallow"),
        "reason": reason,
    }
    if reason:
        settle(url_id, "released", reason)
        with tx() as conn:
            conn.execute(
                """UPDATE urls SET status = 'not_sendable', queue_status = 'skipped', queue_note = ?,
                   in_hub = 0, precheck_json = ?, http_status = ?, final_url = ?, noindex = ?,
                   canonical = ?, normalized_url = ?, host = ?, kind = ?, last_checked_at = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    reason[:500],
                    json.dumps(payload),
                    result.get("http_status"),
                    result.get("final_url"),
                    1 if result.get("noindex") else 0,
                    result.get("canonical"),
                    final,
                    host_of(final),
                    kind,
                    now,
                    now,
                    url_id,
                ),
            )
        network.log_event(url_id, "precheck", payload)
        return {"bucket": "not_sendable", "item": {"url": row["url"], "reason": reason}}
    with tx() as conn:
        conn.execute(
            """UPDATE urls SET precheck_json = ?, http_status = ?, final_url = ?, is_live = ?,
               noindex = ?, canonical = ?, normalized_url = ?, host = ?, kind = ?,
               last_checked_at = ?, updated_at = ?, status = 'queued'
               WHERE id = ? AND status NOT IN ('not_sendable', 'indexed')""",
            (
                json.dumps(payload),
                result.get("http_status"),
                result.get("final_url"),
                1 if result.get("is_live") else 0,
                1 if result.get("noindex") else 0,
                result.get("canonical"),
                final,
                host_of(final),
                kind,
                now,
                now,
                url_id,
            ),
        )
    network.log_event(url_id, "precheck", payload)
    return {"bucket": "accepted", "item": {"id": url_id, "url": final, "status": "queued", "kind": kind}}


async def run_precheck() -> dict[str, Any]:
    rows = get_conn().execute(
        """SELECT id FROM urls
           WHERE status = 'queued' AND (precheck_json IS NULL OR precheck_json = '')
           ORDER BY id LIMIT 4"""
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    result = await precheck_ids(ids)
    errors = list(result.pop("errors", []))
    return {"checked": len(ids), **{key: len(value) for key, value in result.items()}, "errors": errors}


def _submitted_today(project_id: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM urls WHERE project_id = ? AND submitted_at LIKE ?",
        (project_id, utcnow()[:10] + "%"),
    ).fetchone()
    return int(row["c"] or 0)


def _due_external() -> dict[int, list[dict]]:
    rows = get_conn().execute(
        """SELECT u.*, p.daily_speed
           FROM urls u JOIN projects p ON p.id = u.project_id
           WHERE p.run_status = 'running'
             AND p.project_type IN ('indexing', 'instant')
             AND u.kind = 'external'
             AND u.status = 'queued'
             AND u.precheck_json != ''
             AND (u.hub_page_id IS NULL OR u.hub_page_id = 0)
           ORDER BY u.id"""
    ).fetchall()
    grouped: dict[int, list[dict]] = {}
    room: dict[int, int] = {}
    for row in rows:
        project_id = int(row["project_id"])
        if project_id not in room:
            room[project_id] = max(0, int(row["daily_speed"] or 0) - _submitted_today(project_id))
        if room[project_id] <= 0:
            continue
        grouped.setdefault(project_id, []).append(dict(row))
        room[project_id] -= 1
    return grouped


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _backoff(domain_id: int) -> bool:
    rows = get_conn().execute(
        """SELECT type, payload_json, created_at FROM events
           WHERE type IN ('indexnow_push', 'google_push')
           ORDER BY id DESC LIMIT 30"""
    ).fetchall()
    failures = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if int(payload.get("domain_id") or 0) != int(domain_id):
            continue
        if payload.get("ok"):
            break
        status = payload.get("status")
        if status is not None and int(status) < 500 and "fail" not in str(payload.get("detail") or "").lower():
            continue
        failures.append(row["created_at"])
    if not failures:
        return False
    wait = min(6 * 3600, 30 * (2 ** (len(failures) - 1)))
    last = _parse(failures[0])
    if last and datetime.now(timezone.utc) < last + timedelta(seconds=wait):
        if len(failures) >= 3:
            _notify_operators(
                f"Indexer network push failed {len(failures)} times for hub domain {domain_id}. "
                "The latest responses are in the push log."
            )
        return True
    return False


def _notify_operators(message: str) -> None:
    message = message[:500]
    day = utcnow()[:10]
    users = get_conn().execute("SELECT id, email, username, is_admin FROM users").fetchall()
    for row in users:
        email = normalize_email(row["email"] or "")
        if not email:
            email = operator_email_for_username(row["username"] or "")
        if not (row["is_admin"] or is_unlimited_email(email)):
            continue
        existing = get_conn().execute(
            """SELECT id FROM notifications
               WHERE user_id = ? AND message = ? AND created_at >= ?""",
            (row["id"], message, day),
        ).fetchone()
        if existing:
            continue
        with tx() as conn:
            conn.execute(
                "INSERT INTO notifications (user_id, message, created_at) VALUES (?, ?, ?)",
                (row["id"], message, utcnow()),
            )


def _revoked(result: dict) -> bool:
    status = int(result.get("status") or 0)
    text = (result.get("detail") or "").lower()
    return status in {403, 429} and any(word in text for word in ("quota", "revok", "permission", "forbidden"))


async def _push_page(client: httpx.AsyncClient, page: dict, domain: Optional[dict], target: str) -> None:
    url_ids = []
    try:
        url_ids = [int(item) for item in json.loads(page.get("url_ids_json") or "[]")]
    except json.JSONDecodeError:
        url_ids = []
    if domain:
        if not domain.get("indexnow_verified_at"):
            network.mark_domain_result(domain["id"], ok=False, detail="IndexNow key is not verified yet.")
            return
        if _backoff(domain["id"]):
            return
        if not network.take_indexnow_slot(domain["id"]):
            network.mark_domain_result(domain["id"], ok=False, detail="IndexNow daily quota is used.")
            return
        key = domain["indexnow_key"]
        key_location = f"https://{domain['hostname']}/{key}.txt"
        result = await indexnow.submit(client, [target], key, key_location)
        for url_id in url_ids:
            network.log_event(url_id, "indexnow_push", {"domain_id": domain["id"], "url": target, **result})
        if not result.get("ok"):
            network.mark_domain_result(domain["id"], ok=False, detail=result.get("detail") or "IndexNow rejected the hub page.")
            if int(result.get("status") or 0) >= 500 or result.get("status") is None:
                return
        else:
            network.remember_successful_push(page["id"])
            network.mark_domain_result(domain["id"], ok=True, detail="")
        if domain.get("google_push_enabled"):
            await _push_google(client, domain, page, target, url_ids)
        return
    key, _source = setting_or_env("indexnow_key", "INDEXNOW_KEY")
    base = target.split("/l/")[0] if "/l/" in target else ""
    if not key or not base:
        for url_id in url_ids:
            network.log_event(url_id, "indexnow_push", {"ok": False, "detail": "Fallback hub has no IndexNow key.", "url": target})
        return
    if global_quota_remaining("indexnow") < 1 or not consume_global_quota("indexnow", 1):
        return
    result = await indexnow.submit(client, [target], key, f"{base}/{key}.txt")
    for url_id in url_ids:
        network.log_event(url_id, "indexnow_push", {"domain_id": 0, "url": target, **result})
    if result.get("ok"):
        network.remember_successful_push(page["id"])


async def _push_google(client, domain: dict, page: dict, target: str, url_ids: list[int]) -> None:
    slot = network.take_google_slot(domain["id"])
    if not slot:
        network.mark_domain_result(domain["id"], ok=False, detail="Google Indexing API daily quota is used.")
        return
    _index, info = slot
    try:
        token = google_indexing.access_token_for(info)
    except Exception as exc:
        result = {"ok": False, "status": None, "detail": f"Could not sign a Google token: {exc}"}
        token = ""
    else:
        result = await google_indexing.publish_notification(client, token, target, "URL_UPDATED")
    for url_id in url_ids:
        network.log_event(
            url_id,
            "google_push",
            {"domain_id": domain["id"], "url": target, "ok": result.get("ok"), "status": result.get("status"), "detail": result.get("detail")},
        )
    if result.get("ok"):
        network.mark_domain_result(domain["id"], ok=True, detail="")
        return
    network.mark_domain_result(domain["id"], ok=False, detail=result.get("detail") or "Google Indexing API rejected the hub page.")
    if _revoked(result):
        _notify_operators(
            f"Google Indexing API returned an error for {domain['hostname']}: {result.get('detail', '')[:180]}. "
            "Quota for that Cloud project may have been revoked."
        )


def _public_base() -> str:
    from .crawlhub import crawl_base

    return crawl_base("")


async def run_drip(client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    network.reset_quotas_if_needed()
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=8)
    created = 0
    pushed = 0
    errors: list[dict] = []
    deadline = time.monotonic() + _job_seconds()
    page_cap = 2
    try:
        domains = [domain for domain in network.list_domains() if domain.get("indexnow_verified_at")]
        try:
            cursor = int(get_setting("hub_rr_cursor") or "0")
        except ValueError:
            cursor = 0
        base = _public_base()
        stop = False
        for _project_id, rows in _due_external().items():
            for chunk in _chunks(rows, network.PAGE_URLS):
                if created >= page_cap or time.monotonic() >= deadline:
                    stop = True
                    break
                url_ids = [int(row["id"]) for row in chunk]
                try:
                    domain = _pick_domain(domains, cursor)
                    cursor += 1
                    page = network.create_page(domain, url_ids)
                    target = network.page_http_url(domain, page["slug"], base)
                    await _push_page(client, page, domain, target)
                except Exception as exc:
                    _note_failure(errors, exc, url_ids=url_ids)
                    continue
                created += 1
                pushed += 1
            if stop:
                break
        set_setting("hub_rr_cursor", str(cursor))
        if time.monotonic() < deadline:
            await _repush(client, base, limit=1, errors=errors)
        if time.monotonic() < deadline:
            await _submit_own(client, limit=4, errors=errors)
    finally:
        if owns:
            await client.aclose()
    return {"pages": created, "pushed": pushed, "errors": errors}


def _pick_domain(domains: list[dict], cursor: int) -> Optional[dict]:
    if not domains:
        return None
    for offset in range(len(domains)):
        domain = domains[(cursor + offset) % len(domains)]
        snap = network.quota_snapshot(domain)
        if snap["indexnow_remaining"] < 1:
            continue
        if _backoff(domain["id"]):
            continue
        return domain
    return None


def _job_seconds() -> float:
    """Stay inside a Hobby function's default 10 second limit."""
    try:
        budget = float(os.environ.get("CRON_BUDGET_SECONDS", "8"))
    except ValueError:
        budget = 8.0
    return max(3.0, min(9.0, budget))


async def _repush(client: httpx.AsyncClient, base: str, limit: int = 1, errors: Optional[list[dict]] = None) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = get_conn().execute(
        """SELECT * FROM hub_pages
           WHERE pushes_count < 2 AND pushes_count >= 0"""
    ).fetchall()
    sent = 0
    for page in pages:
        if sent >= limit:
            break
        page = dict(page)
        count = int(page.get("pushes_count") or 0)
        if count == 0:
            created_at = _parse(page.get("created_at") or "")
            if created_at and datetime.now(timezone.utc) - created_at < timedelta(minutes=10):
                continue
        elif count == 1 and (page.get("last_pushed_at") or "") <= cutoff:
            ids = []
            try:
                ids = [int(item) for item in json.loads(page.get("url_ids_json") or "[]")]
            except json.JSONDecodeError:
                ids = []
            if not ids:
                continue
            placeholders = ",".join("?" * len(ids))
            hit = get_conn().execute(
                f"""SELECT id FROM urls WHERE id IN ({placeholders})
                    AND status IN ('discovered', 'indexed')""",
                ids,
            ).fetchone()
            if hit:
                continue
        else:
            continue
        try:
            domain = network.domain_by_id(page["hub_domain_id"]) if page.get("hub_domain_id") else None
            target = network.page_http_url(domain, page["slug"], base)
            await _push_page(client, page, domain, target)
        except Exception as exc:
            if errors is not None:
                _note_failure(errors, exc, page_id=int(page["id"]))
            else:
                raise
            continue
        sent += 1


async def _submit_own(client: httpx.AsyncClient, limit: int = 4, errors: Optional[list[dict]] = None) -> None:
    rows = get_conn().execute(
        """SELECT u.*, p.user_id, p.daily_speed
           FROM urls u JOIN projects p ON p.id = u.project_id
           WHERE p.run_status = 'running'
             AND p.project_type IN ('indexing', 'instant')
             AND u.kind = 'own'
             AND u.status = 'queued'
             AND u.precheck_json != ''
           ORDER BY u.id LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    room: dict[int, int] = {}
    for row in rows:
        project_id = int(row["project_id"])
        if project_id not in room:
            room[project_id] = max(0, int(row["daily_speed"] or 0) - _submitted_today(project_id))
        if room[project_id] <= 0:
            continue
        try:
            host = row.get("host") or host_of(row["url"])
            config = gsc.indexnow_for(int(row["user_id"]), host)
            if not config.get("key") or not config.get("verified_at"):
                network.log_event(int(row["id"]), "indexnow_push", {"ok": False, "detail": "Own host has no verified IndexNow key."})
                continue
            key_location = f"https://{host}/{config['key']}.txt"
            result = await indexnow.submit(client, [row["url"]], config["key"], key_location)
            network.log_event(int(row["id"]), "indexnow_push", {"url": row["url"], **result})
            if not result.get("ok"):
                continue
            now = utcnow()
            with tx() as conn:
                conn.execute(
                    """UPDATE urls SET status = 'submitted',
                       submitted_at = CASE WHEN submitted_at IS NULL OR submitted_at = '' THEN ? ELSE submitted_at END,
                       updated_at = ? WHERE id = ? AND status = 'queued'""",
                    (now, now, row["id"]),
                )
            room[project_id] -= 1
        except Exception as exc:
            if errors is not None:
                _note_failure(errors, exc, url_id=int(row["id"]))
            else:
                raise


def serpapi_key() -> str:
    provider = (get_setting("serp_provider") or "serpapi").lower()
    if provider == "serpapi":
        return setting_or_env("serp_api_key", "SERPAPI_KEY")[0]
    return (os.environ.get("SERPAPI_KEY") or "").strip()


def serp_remaining() -> tuple[int, int]:
    try:
        limit = max(0, int(get_setting("serp_daily_budget") or "100"))
    except ValueError:
        limit = 100
    today = utcnow()[:10]
    if get_setting("serp_budget_date") != today:
        return limit, limit
    try:
        used = int(get_setting("serp_budget_count") or "0")
    except ValueError:
        used = 0
    return max(0, limit - used), limit


def _take_serp(count: int = 1) -> bool:
    left, limit = serp_remaining()
    if left < count:
        return False
    today = utcnow()[:10]
    used = limit - left
    with tx() as conn:
        for key, value in (("serp_budget_date", today), ("serp_budget_count", str(used + count))):
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
    return True


def _cache_get(key: str) -> Optional[dict]:
    row = get_conn().execute("SELECT payload, created_at FROM serp_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    created = _parse(row["created_at"])
    if not created or datetime.now(timezone.utc) - created > timedelta(hours=24):
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def _cache_put(key: str, payload: dict) -> None:
    raw = json.dumps(payload)
    now = utcnow()
    with tx() as conn:
        conn.execute(
            """INSERT INTO serp_cache (cache_key, payload, created_at) VALUES (?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at""",
            (key, raw, now),
        )


async def cached_search(client, engine: str, query: str, match_url: str) -> dict:
    key_name = f"{engine}|{query}"
    cached = _cache_get(key_name)
    if cached:
        cached = dict(cached)
        cached["cached"] = True
        return cached
    api_key = serpapi_key()
    if not api_key:
        return {"ok": False, "indexed": None, "detail": "No SerpApi key is configured.", "engine": engine, "query": query}
    if not _take_serp(1):
        return {"ok": False, "indexed": None, "detail": "Daily SerpApi budget is used.", "engine": engine, "query": query}
    result = await serp.serpapi_search(client, api_key, engine, query, match_url)
    if result.get("ok"):
        _cache_put(key_name, result)
    return result


async def check_url_serps(client, url: str) -> dict[str, Any]:
    google = await cached_search(client, "google", serp.build_query(url), url)
    if google.get("ok") and google.get("indexed") is False:
        fallback = await cached_search(client, "google", serp.inurl_query(url), url)
        if fallback.get("ok"):
            google = fallback
    bing = await cached_search(client, "bing", serp.build_query(url), url)
    yandex = await cached_search(client, "yandex", serp.build_query(url), url)
    return {"google": google, "bing": bing, "yandex": yandex}


def _schedule_next(created_at: str, checked_at: str) -> str:
    anchor = _parse(created_at)
    checked = _parse(checked_at)
    if not anchor or not checked:
        return ""
    for day in CHECK_DAYS:
        moment = anchor + timedelta(days=day)
        if moment > checked:
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def apply_serp(url_id: int, results: dict[str, Any]) -> None:
    row = get_conn().execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    if not row:
        return
    now = utcnow()
    indexed_any = False
    updates: dict[str, str] = {}
    for engine, column in (("google", "google_state"), ("bing", "bing_state"), ("yandex", "yandex_state")):
        result = results.get(engine) or {}
        if result.get("unverified"):
            updates[column] = "unverified"
            continue
        if not result.get("ok") or result.get("indexed") is None:
            continue
        if result.get("indexed"):
            updates[column] = "indexed"
            indexed_any = True
        elif (row.get(column) or "") != "indexed":
            updates[column] = "not_indexed"
    nxt = "" if indexed_any else _schedule_next(row["created_at"], now)
    terminal = ""
    if not indexed_any and not nxt and (row.get("status") or "") not in {"indexed", "not_sendable"}:
        anchor = _parse(row["created_at"])
        checked = _parse(now)
        if anchor and checked and checked >= anchor + timedelta(days=28):
            terminal = "not_indexed_28d"
    with tx() as conn:
        assignments = ["last_check_at = ?", "next_check_at = ?", "updated_at = ?"]
        params: list[Any] = [now, nxt, now]
        for column, value in updates.items():
            assignments.append(f"{column} = ?")
            params.append(value)
        if updates.get("google_state") in {"indexed", "not_indexed"}:
            assignments.append("index_source = ?")
            params.append("serpapi")
            assignments.append("last_index_check_at = ?")
            params.append(now)
        if terminal:
            assignments.append("status = ?")
            params.append(terminal)
        params.append(url_id)
        conn.execute(f"UPDATE urls SET {', '.join(assignments)} WHERE id = ?", params)
    network.log_event(url_id, "serp_check", {"indexed": indexed_any, "engines": {key: (value or {}).get("indexed") for key, value in results.items()}})
    if indexed_any:
        confirm_indexed(url_id)


async def run_serp_checks(client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    refunded = refund_expired()
    rows = get_conn().execute(
        """SELECT id, url FROM urls
           WHERE next_check_at != '' AND next_check_at <= ?
             AND status NOT IN ('not_sendable', 'indexed', 'not_indexed_28d')
           ORDER BY next_check_at LIMIT 3""",
        (utcnow(),),
    ).fetchall()
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=5)
    checked = 0
    errors: list[dict] = []
    deadline = time.monotonic() + _job_seconds()
    try:
        for row in rows:
            if time.monotonic() >= deadline:
                break
            if serp_remaining()[0] <= 0 and not _cache_get("google|" + serp.build_query(row["url"])):
                break
            try:
                results = await check_url_serps(client, row["url"])
                apply_serp(int(row["id"]), results)
            except Exception as exc:
                _note_failure(errors, exc, url_id=int(row["id"]))
                continue
            checked += 1
    finally:
        if owns:
            await client.aclose()
    return {"checked": checked, "refunded": refunded, "errors": errors}


async def run_inspection(client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    rows = get_conn().execute(
        """SELECT u.*, p.user_id FROM urls u
           JOIN projects p ON p.id = u.project_id
           WHERE u.kind = 'own' AND u.status IN ('submitted', 'discovered')
           ORDER BY u.id LIMIT 3"""
    ).fetchall()
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=5)
    checked = 0
    errors: list[dict] = []
    deadline = time.monotonic() + _job_seconds()
    try:
        for row in rows:
            if time.monotonic() >= deadline:
                break
            try:
                user_id = int(row["user_id"])
                config = await gsc.authorized_config(client, user_id)
                prop = gsc.matching_property(row.get("host") or host_of(row["url"]), config.get("properties") or [])
                if not prop:
                    continue
                if not gsc.inspect_quota_take(user_id, prop):
                    continue
                result = await gsc.inspect_url(client, config, prop, row["url"])
                network.log_event(int(row["id"]), "inspection", {"ok": result.get("ok"), "crawled": result.get("crawled"), "detail": result.get("detail")})
                if result.get("crawled") and row.get("status") == "submitted":
                    with tx() as conn:
                        conn.execute(
                            "UPDATE urls SET status = 'discovered', updated_at = ? WHERE id = ? AND status = 'submitted'",
                            (utcnow(), row["id"]),
                        )
                if not result.get("ok") and int(result.get("status") or 0) >= 500:
                    _notify_operators(f"URL Inspection failed for {row['url']}: {result.get('detail', '')[:160]}")
            except Exception as exc:
                _note_failure(errors, exc, url_id=int(row["id"]))
                continue
            checked += 1
    finally:
        if owns:
            await client.aclose()
    return {"checked": checked, "errors": errors}


def run_quota_reset() -> dict[str, Any]:
    cleared = network.reset_quotas_if_needed()
    today = utcnow()[:10]
    with tx() as conn:
        for key, value in (("serp_budget_date", today), ("serp_budget_count", "0")):
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
    refunded = refund_expired()
    return {"domains": cleared, "refunded": refunded}


def user_status_label(row: dict) -> str:
    status = (row.get("status") or "queued").strip() or "queued"
    credits_state = row.get("credits_state") or ""
    if status == "not_sendable":
        return "Not indexable (refunded)"
    if status == "indexed":
        return "Indexed"
    if credits_state == "refunded":
        return "Refunded"
    return {
        "queued": "In Queue",
        "submitted": "Submitted",
        "discovered": "Crawled",
        "not_indexed_28d": "Refunded" if credits_state == "refunded" else "Not indexed 28d",
    }.get(status, status)


def status_tooltip(row: dict, hub_url: str) -> str:
    parts = [f"Internal status: {row.get('status') or 'queued'}"]
    reason = row.get("queue_note") or ""
    if row.get("precheck_json"):
        try:
            payload = json.loads(row["precheck_json"])
            reason = payload.get("reason") or reason
        except json.JSONDecodeError:
            pass
    if reason:
        parts.append(str(reason))
    if hub_url:
        parts.append(f"Hub page: {hub_url}")
    if row.get("last_check_at"):
        parts.append(f"Last check: {row['last_check_at']}")
    if row.get("credits_state") == "refunded":
        parts.append("Credits were refunded. A later index confirmation is not charged again.")
    return " · ".join(parts)


def hub_url_for(row: dict, public_base: str) -> str:
    if not row.get("hub_page_id"):
        return ""
    page = network.page_by_id(int(row["hub_page_id"]))
    if not page:
        return ""
    domain = network.domain_by_id(page["hub_domain_id"]) if page.get("hub_domain_id") else None
    return network.page_http_url(domain, page["slug"], public_base)


def decorate_url(row: dict, public_base: str) -> dict:
    item = dict(row)
    hub_url = hub_url_for(item, public_base)
    item["hub_page_url"] = hub_url
    item["status_label"] = user_status_label(item)
    item["status_tip"] = status_tooltip(item, hub_url)
    item["refunded_flag"] = (item.get("credits_state") or "") == "refunded"
    page = network.page_by_id(int(item["hub_page_id"])) if item.get("hub_page_id") else None
    item["pushes"] = int(page["pushes_count"]) if page else 0
    if item.get("kind") == "own":
        props = []
        try:
            user_row = get_conn().execute(
                "SELECT user_id FROM projects WHERE id = ?",
                (item["project_id"],),
            ).fetchone()
            if user_row:
                props = gsc.verified_properties(int(user_row["user_id"]))
        except Exception:
            props = []
        prop = gsc.matching_property(item.get("host") or host_of(item.get("url") or ""), props)
        item["inspection_href"] = gsc.inspection_href(prop, item.get("url") or "")
    else:
        item["inspection_href"] = ""
    return item


def campaign_report(project_id: int) -> dict[str, Any]:
    status_rows = get_conn().execute(
        """SELECT COALESCE(NULLIF(status, ''), 'queued') AS status, COUNT(*) AS c
           FROM urls WHERE project_id = ? GROUP BY COALESCE(NULLIF(status, ''), 'queued')""",
        (project_id,),
    ).fetchall()
    by_status = {key: 0 for key in ("queued", "not_sendable", "submitted", "discovered", "indexed", "not_indexed_28d")}
    total = 0
    for row in status_rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + int(row["c"] or 0)
        total += int(row["c"] or 0)
    engines = {}
    for name, column in (("google", "google_state"), ("bing", "bing_state"), ("yandex", "yandex_state")):
        indexed = get_conn().execute(
            f"SELECT COUNT(*) AS c FROM urls WHERE project_id = ? AND {column} = 'indexed'",
            (project_id,),
        ).fetchone()
        submitted = get_conn().execute(
            f"""SELECT COUNT(*) AS c FROM urls WHERE project_id = ?
                AND {column} IN ('accepted', 'submitted', 'external_hub')""",
            (project_id,),
        ).fetchone()
        engines[name] = {"indexed": int(indexed["c"] or 0), "submitted": int(submitted["c"] or 0)}
    indexed_count = by_status.get("indexed", 0)
    avg = _avg_days(project_id)
    credits = summary_for_project(project_id)
    return {
        "counts": {"by_status": by_status, "by_engine": engines, "total": total},
        "indexed_rate": round(indexed_count / total, 4) if total else 0.0,
        "avg_days_to_index": avg,
        "refunded": credits["refunded"],
        "credits": credits,
    }


def _avg_days(project_id: int) -> Optional[float]:
    rows = get_conn().execute(
        """SELECT created_at, indexed_at FROM urls
           WHERE project_id = ? AND status = 'indexed' AND indexed_at != ''""",
        (project_id,),
    ).fetchall()
    samples = []
    for row in rows:
        start = _parse(row["created_at"])
        end = _parse(row["indexed_at"])
        if start and end:
            samples.append((end - start).total_seconds() / 86400)
    if not samples:
        return None
    return round(sum(samples) / len(samples), 2)


def list_campaign_urls(project_id: int, status: str, page: int, per: int, public_base: str) -> dict[str, Any]:
    per = min(100, max(1, per or 50))
    page = max(1, page or 1)
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        where.append("status = ?")
        params.append(status)
    sql_where = " AND ".join(where)
    total = int(get_conn().execute(f"SELECT COUNT(*) AS c FROM urls WHERE {sql_where}", params).fetchone()["c"])
    rows = get_conn().execute(
        f"""SELECT * FROM urls WHERE {sql_where}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        [*params, per, (page - 1) * per],
    ).fetchall()
    items = []
    for row in rows:
        item = decorate_url(dict(row), public_base)
        items.append(
            {
                "id": item["id"],
                "url": item["url"],
                "normalized_url": item.get("normalized_url") or "",
                "kind": item.get("kind") or "",
                "status": item.get("status") or "queued",
                "status_label": item["status_label"],
                "reason": item.get("queue_note") or "",
                "hub_page_url": item.get("hub_page_url") or "",
                "pushes": item.get("pushes") or 0,
                "last_check_at": item.get("last_check_at") or "",
                "next_check_at": item.get("next_check_at") or "",
                "google_state": item.get("google_state") or "",
                "bing_state": item.get("bing_state") or "",
                "yandex_state": item.get("yandex_state") or "",
                "credits_state": item.get("credits_state") or "",
                "refunded": bool(item.get("refunded_flag")),
                "inspection_href": item.get("inspection_href") or "",
            }
        )
    return {"urls": items, "page": page, "per": per, "total": total}


async def recheck_url(user_id: int, url_id: int, client: Optional[httpx.AsyncClient] = None) -> tuple[int, dict]:
    row = get_conn().execute(
        """SELECT u.* FROM urls u JOIN projects p ON p.id = u.project_id
           WHERE u.id = ? AND p.user_id = ?""",
        (url_id, user_id),
    ).fetchone()
    if not row:
        return 404, {"detail": "URL not found."}
    last = _parse(row.get("last_check_at") or "")
    if last and datetime.now(timezone.utc) - last < timedelta(hours=24):
        return 429, {"detail": "This URL was checked in the last 24 hours."}
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        results = await check_url_serps(client, row["url"])
    finally:
        if owns:
            await client.aclose()
    apply_serp(url_id, results)
    fresh = get_conn().execute("SELECT * FROM urls WHERE id = ?", (url_id,)).fetchone()
    return 200, decorate_url(dict(fresh), _public_base())


async def check_loose(user: dict, urls: list[str], client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Index checker. Charges the configured rate only when an answer comes back."""
    from .credits import charge_checker, checker_cost

    cost = checker_cost(user)
    prepared = []
    seen = set()
    for raw in urls:
        url = canonicalize(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        prepared.append(url)
    prepared = prepared[:20]
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    answers = []
    charged = 0
    try:
        for url in prepared:
            results = await check_url_serps(client, url)
            answered = any((results.get(engine) or {}).get("ok") for engine in ("google", "bing", "yandex"))
            if answered and cost:
                if not charge_checker(int(user["id"]), cost):
                    answers.append({"url": url, "error": "Not enough credits for another check."})
                    break
                charged += cost
            row = {"url": url, "charged": cost if answered else 0, "engines": {}}
            for engine in ("google", "bing", "yandex"):
                result = results.get(engine) or {}
                if result.get("unverified"):
                    row["engines"][engine] = "unverified"
                elif result.get("ok") and result.get("indexed") is True:
                    row["engines"][engine] = "indexed"
                elif result.get("ok") and result.get("indexed") is False:
                    row["engines"][engine] = "not_found"
                else:
                    row["engines"][engine] = "unknown"
                row.setdefault("details", {})[engine] = result.get("detail") or ""
            answers.append(row)
    finally:
        if owns:
            await client.aclose()
    left, limit = serp_remaining()
    return {"results": answers, "credits_charged": charged, "serp_remaining": left, "serp_limit": limit}


async def finalize_intake(result: dict) -> dict:
    """Run the live pre-check when it is enabled. Tests skip it."""
    if not result or not live_precheck_enabled() or not result.get("ids"):
        return result
    extra = await precheck_ids(result.get("ids") or [])
    merged = dict(result)
    merged["accepted"] = extra["accepted"]
    merged["not_sendable"] = extra["not_sendable"]
    merged["skipped_duplicates"] = list(result.get("skipped_duplicates") or []) + list(extra["skipped_duplicates"])
    merged["duplicates"] = len(merged["skipped_duplicates"])
    merged["added"] = len(extra["accepted"])
    return merged
