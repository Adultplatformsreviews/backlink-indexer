"""Daily queue. Submission is never reported as a confirmed index."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .config import FETCH_CONCURRENCY, PUBLIC_BASE_URL, USER_AGENT
from .database import get_conn, get_setting, get_settings, set_setting, tx, utcnow
from .services import bing_webmaster, google_indexing, indexnow, serp, websub, yandex_webmaster
from .services.quotas import consume as consume_quota
from .services.quotas import remaining as quota_remaining
from .services.secrets import setting_or_env
from .services.precheck import RobotsCache, precheck_url
from .services.urlutil import host_of, registrable_host

ENGINES = ("google", "yandex", "bing")


def ticks_per_day() -> int:
    try:
        return max(1, int(os.environ.get("CRON_TICKS_PER_DAY", "1")))
    except ValueError:
        return 1


def budget_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get("QUEUE_BUDGET_SECONDS", "240")))
    except ValueError:
        return 240.0


def _today() -> str:
    return utcnow()[:10]


def _iso_minutes_ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def allowance(project: dict) -> int:
    """How many queued links this run may touch without passing the daily speed."""
    speed = max(0, int(project.get("daily_speed") or 0))
    if speed == 0 or project.get("run_status") != "running":
        return 0
    sent_today = int(project.get("sent_today") or 0) if project.get("sent_on") == _today() else 0
    remaining = max(0, speed - sent_today)
    per_tick = max(1, math.ceil(speed / ticks_per_day()))
    return min(remaining, per_tick)


def _owned_hosts() -> set[str]:
    rows = get_conn().execute("SELECT host FROM owned_hosts").fetchall()
    hosts = {registrable_host(row["host"]) for row in rows}
    base = get_setting("public_base_url") or PUBLIC_BASE_URL
    if base:
        hosts.add(registrable_host(host_of(base)))
    return {host for host in hosts if host}


def _is_owned(url: str, owned: set[str]) -> bool:
    host = registrable_host(host_of(url))
    return any(host == item or host.endswith("." + item) for item in owned)


def _public_base() -> str:
    return (get_setting("public_base_url") or PUBLIC_BASE_URL or "").rstrip("/")


def _index_status(google_state: str, source: str) -> str:
    if google_state == "indexed":
        return "manual_indexed" if source == "manual" else "indexed"
    if google_state == "not_indexed":
        return "manual_not_indexed" if source == "manual" else "not_indexed"
    return "unknown"


def _notify(user_id: int, message: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO notifications (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id, message[:500], utcnow()),
        )


def release_stuck() -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE urls SET queue_status = 'queued', updated_at = ?
               WHERE queue_status = 'processing' AND updated_at < ?""",
            (utcnow(), _iso_minutes_ago(20)),
        )


def _save_url(
    url_id: int,
    project_id: int,
    *,
    precheck: Optional[dict],
    queue_status: str,
    note: str,
    engines: dict[str, str],
    submission_status: str,
    index_source: str = "",
    count_toward_speed: bool = True,
) -> None:
    now = utcnow()
    google_state = engines.get("google") or "unknown"
    with tx() as conn:
        if precheck is None:
            conn.execute(
                """UPDATE urls SET
                    queue_status=?, queue_note=?, google_state=?, yandex_state=?, bing_state=?,
                    submission_status=?, index_status=?, index_source=?, index_note=?,
                    updated_at=?
                   WHERE id=?""",
                (
                    queue_status,
                    note[:500],
                    google_state,
                    engines.get("yandex") or "unknown",
                    engines.get("bing") or "unknown",
                    submission_status,
                    _index_status(google_state, index_source),
                    index_source,
                    note[:500],
                    now,
                    url_id,
                ),
            )
        else:
            submitted = now if queue_status == "sent" else None
            conn.execute(
                """UPDATE urls SET
                    final_url=?, http_status=?, is_live=?, fetch_error=?, content_type=?,
                    meta_robots=?, x_robots_tag=?, noindex=?, robots_disallow=?,
                    canonical=?, canonicalized_away=?, has_link=?, link_rel=?,
                    link_count=?, link_details=?, last_checked_at=?,
                    queue_status=?, queue_note=?, google_state=?, yandex_state=?, bing_state=?,
                    submission_status=?, index_status=?, index_source=?, index_note=?,
                    google_index_status=?, indexnow_status=?,
                    last_submitted_at=COALESCE(?, last_submitted_at),
                    last_index_check_at=CASE WHEN ? != '' THEN ? ELSE last_index_check_at END,
                    updated_at=?
                   WHERE id=?""",
                (
                    precheck.get("final_url"),
                    precheck.get("http_status"),
                    1 if precheck.get("is_live") else 0 if precheck.get("is_live") is not None else None,
                    precheck.get("fetch_error"),
                    precheck.get("content_type"),
                    precheck.get("meta_robots"),
                    precheck.get("x_robots_tag"),
                    1 if precheck.get("noindex") else 0,
                    None
                    if precheck.get("robots_disallow") is None
                    else (1 if precheck.get("robots_disallow") else 0),
                    precheck.get("canonical"),
                    1 if precheck.get("canonicalized_away") else 0,
                    1 if precheck.get("has_link") else 0,
                    precheck.get("link_rel") or "none",
                    precheck.get("link_count") or 0,
                    precheck.get("link_details_json") or "[]",
                    now,
                    queue_status,
                    note[:500],
                    google_state,
                    engines.get("yandex") or "unknown",
                    engines.get("bing") or "unknown",
                    submission_status,
                    _index_status(google_state, index_source),
                    index_source,
                    note[:500],
                    precheck.get("google_detail") or "",
                    precheck.get("indexnow_detail") or "",
                    submitted,
                    index_source if google_state in {"indexed", "not_indexed"} else "",
                    now if google_state in {"indexed", "not_indexed"} else None,
                    now,
                    url_id,
                ),
            )
        if count_toward_speed:
            row = conn.execute("SELECT sent_today, sent_on FROM projects WHERE id=?", (project_id,)).fetchone()
            today = _today()
            current = int(row["sent_today"] or 0) if row and row["sent_on"] == today else 0
            conn.execute(
                "UPDATE projects SET sent_today=?, sent_on=?, updated_at=? WHERE id=?",
                (current + 1, today, now, project_id),
            )


def _prefer(current: str, new: str) -> str:
    """Keep an acceptance if any real submission was accepted. Never upgrade to indexed."""
    if new == "accepted" or current in {"", "unknown"}:
        return new or current
    if current == "accepted":
        return current
    return new or current


async def _submit_owned(
    client: httpx.AsyncClient,
    url: str,
    owned: set[str],
    base: str,
    key: str,
) -> tuple[dict[str, str], str, dict[str, str]]:
    """Return engine states, note, and extra detail fields. Never sets indexed."""
    engines = {"google": "unknown", "yandex": "unknown", "bing": "unknown"}
    extra = {"google_detail": "", "indexnow_detail": ""}
    notes: list[str] = []
    if not _is_owned(url, owned):
        return engines, "", extra
    host = host_of(url)
    deferred = False
    if key:
        if quota_remaining("indexnow") < 1:
            deferred = True
            notes.append("IndexNow fair-use quota is used for today. This URL stays queued for the next day.")
            results = {}
        else:
            key_location = f"https://{host}/{key}.txt"
            if base and registrable_host(host_of(base)) == registrable_host(host):
                key_location = indexnow.key_file_url(base, key)
            results = await indexnow.submit_all(client, [url], key, key_location)
            consume_quota("indexnow", 1)
            bing_now = results.get("bing") or {}
            yandex_now = results.get("yandex") or {}
            shared = results.get("indexnow") or {}

            def _state(result: dict) -> str:
                if result.get("state") in {"accepted", "rejected"}:
                    return result["state"]
                if result.get("ok"):
                    return "accepted"
                return "rejected"

            engines["bing"] = _prefer("unknown", _state(bing_now) if bing_now else _state(shared))
            engines["yandex"] = _prefer("unknown", _state(yandex_now) if yandex_now else _state(shared))
            extra["indexnow_detail"] = " ".join(
                f"{name}={item.get('detail')}" for name, item in results.items()
            )[:500]
            notes.append("IndexNow: " + extra["indexnow_detail"] + " Acceptance is not an index confirmation.")
    else:
        notes.append("Host is owned, but no IndexNow key is saved in Settings.")

    bing_key, _bing_source = setting_or_env("bing_webmaster_api_key", "BING_WEBMASTER_API_KEY")
    bing_site = (get_setting("bing_site_url") or "").strip() or f"https://{host}/"
    site_host = registrable_host(host_of(bing_site))
    if bing_key and (not site_host or site_host == registrable_host(host)):
        bing_result = await bing_webmaster.submit_url(client, bing_key, bing_site, url)
        notes.append(bing_result.get("detail") or "Bing Webmaster returned no detail.")
        if bing_result.get("state") == "deferred":
            deferred = True
        elif bing_result.get("state") in {"accepted", "rejected"}:
            engines["bing"] = _prefer(engines["bing"], bing_result["state"])
    elif bing_key:
        notes.append("Bing Webmaster key is set, but this URL is not on the verified Bing site URL.")

    token, _token_source = setting_or_env("yandex_webmaster_token", "YANDEX_WEBMASTER_TOKEN")
    user_id, _user_source = setting_or_env("yandex_webmaster_user_id", "YANDEX_WEBMASTER_USER_ID")
    host_id, _host_source = setting_or_env("yandex_webmaster_host_id", "YANDEX_WEBMASTER_HOST_ID")
    yandex_host = registrable_host(get_setting("yandex_site_host") or host)
    if token and user_id and host_id and yandex_host == registrable_host(host):
        quota = await yandex_webmaster.fetch_quota(client, token, user_id, host_id)
        if not quota.get("ok"):
            notes.append(quota.get("detail") or "Yandex recrawl quota could not be read.")
        elif int(quota.get("remainder") or 0) < 1:
            deferred = True
            notes.append("Yandex recrawl daily quota is exhausted. This URL stays queued for the next day.")
        else:
            recrawl = await yandex_webmaster.recrawl_url(client, token, user_id, host_id, url)
            notes.append(recrawl.get("detail") or "Yandex recrawl returned no detail.")
            if recrawl.get("state") in {"accepted", "rejected"}:
                engines["yandex"] = _prefer(engines["yandex"], recrawl["state"])
    elif token or user_id or host_id:
        notes.append("Yandex recrawl needs an OAuth token, user id, and host id. Recrawl was not sent.")

    if google_indexing.service_account_present():
        if quota_remaining("google_indexing") < 1:
            deferred = True
            notes.append("Google Indexing API daily quota (200) is used. This URL stays queued for the next day.")
        else:
            result = google_indexing.publish_url(url)
            extra["google_detail"] = (result.get("detail") or "")[:300]
            if result.get("ok"):
                engines["google"] = "submitted"
                consume_quota("google_indexing", 1)
                notes.append("Google Indexing API accepted the URL. That is not proof it is indexed.")
            else:
                notes.append("Google Indexing API: " + (result.get("detail") or "not accepted."))
    extra["deferred"] = deferred
    return engines, " ".join(notes).strip(), extra


async def _process_row(
    client: httpx.AsyncClient,
    row: dict,
    project: dict,
    owned: set[str],
    robots: RobotsCache,
) -> str:
    now = utcnow()
    with tx() as conn:
        claimed = conn.execute(
            """UPDATE urls SET queue_status='processing', updated_at=?
               WHERE id=? AND queue_status='queued'""",
            (now, row["id"]),
        )
        if claimed.rowcount == 0:
            return "busy"
    try:
        precheck = await precheck_url(
            client,
            row["url"],
            project["target_domain"],
            bool(project.get("include_subdomains", 1)),
            robots,
        )
    except Exception as exc:
        _save_url(
            row["id"],
            project["id"],
            precheck=None,
            queue_status="failed",
            note=f"Pre-check failed: {exc}",
            engines={"google": "unknown", "yandex": "unknown", "bing": "unknown"},
            submission_status="pending",
        )
        return "failed"

    project_type = project.get("project_type") or "indexing"
    engines = {"google": "unknown", "yandex": "unknown", "bing": "unknown"}
    note = ""
    submission = "pending"
    source = ""
    extra: dict[str, str] = {}

    if project_type == "check":
        settings = get_settings()
        provider = settings.get("serp_provider") or "serpapi"
        api_key = settings.get("serp_api_key") or ""
        if api_key or provider == "custom":
            result = await serp.check_indexed(
                client, row["url"], provider, api_key, settings.get("serp_endpoint") or ""
            )
            if result.get("ok") and result.get("indexed") is not None:
                engines["google"] = "indexed" if result["indexed"] else "not_indexed"
                source = provider
                note = result.get("detail") or "Google SERP check finished."
                hits = result.get("hits") or []
                if hits:
                    note += " Hits: " + ", ".join(hits[:3])
            else:
                note = result.get("detail") or "SERP check did not return an index status. Left unknown."
        else:
            note = "No SERP API key is configured. Index status was left unknown."
        submission = "checked" if engines["google"] in {"indexed", "not_indexed"} else "check_pending"
        precheck["google_detail"] = note[:300]
    else:
        base = _public_base()
        key = get_setting("indexnow_key")
        engines, owned_note, extra = await _submit_owned(client, row["url"], owned, base, key)
        if owned_note:
            _record_submission(row["id"], "submission", engines.get("bing") or "unknown", owned_note)
        precheck["google_detail"] = extra.get("google_detail") or ""
        precheck["indexnow_detail"] = extra.get("indexnow_detail") or ""
        if extra.get("deferred"):
            note = owned_note
            _save_url(
                row["id"], project["id"], precheck=precheck, queue_status="queued",
                note=note, engines=engines, submission_status="quota_hold",
                index_source="", count_toward_speed=False,
            )
            with tx() as conn:
                conn.execute(
                    "UPDATE urls SET hold_until=?, updated_at=? WHERE id=?",
                    (_tomorrow(), utcnow(), row["id"]),
                )
            return "deferred"
        if _is_owned(row["url"], owned):
            note = owned_note or "Owned host: no IndexNow key or Google service account is configured. Listed on the discovery hub only."
        elif project_type == "instant":
            note = (
                "Listed on your discovery hub. IndexNow and the Google Indexing API "
                "were not used because this host is not in Owned hosts."
            )
        else:
            note = "Listed on your discovery hub so crawlers can discover it. This is not an index confirmation."
        submission = "submitted_hub"
        with tx() as conn:
            conn.execute("UPDATE urls SET in_hub=1, updated_at=? WHERE id=?", (utcnow(), row["id"]))

    _save_url(
        row["id"],
        project["id"],
        precheck=precheck,
        queue_status="sent",
        note=note,
        engines=engines,
        submission_status=submission,
        index_source=source,
    )
    return "sent"


def _due_projects(user_id: Optional[int], project_id: Optional[int]) -> list[dict]:
    sql = "SELECT * FROM projects WHERE run_status = 'running'"
    params: list[Any] = []
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if project_id is not None:
        sql += " AND id = ?"
        params.append(project_id)
    sql += " ORDER BY id ASC"
    return get_conn().execute(sql, params).fetchall()


def _record_submission(url_id: int, method: str, status: str, detail: str) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO submission_log (url_id, method, status, detail, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (url_id, method, status, (detail or "")[:500], utcnow()),
        )


def _tomorrow() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


def _queued_rows(project_id: int, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    return get_conn().execute(
        """SELECT * FROM urls
           WHERE project_id = ? AND queue_status = 'queued'
             AND (hold_until IS NULL OR hold_until = '' OR hold_until <= ?)
           ORDER BY id ASC LIMIT ?""",
        (project_id, _today(), limit),
    ).fetchall()


async def _maybe_ping(client: httpx.AsyncClient, base: str) -> Optional[dict]:
    host = registrable_host(host_of(base))
    if not base or host in {"", "localhost", "127.0.0.1", "::1"}:
        return None
    if get_setting("last_discovery_ping")[:10] == _today():
        return None
    hub = get_setting("websub_hub") or websub.DEFAULT_HUB
    websub_result = await websub.ping_websub(client, f"{base}/hub/feed.xml", hub)
    bing_result = await websub.ping_bing_sitemap(client, f"{base}/hub/sitemap.xml")
    set_setting("last_discovery_ping", utcnow())
    with tx() as conn:
        conn.execute(
            """INSERT INTO submission_log (url_id, method, status, detail, created_at)
               VALUES (NULL, 'hub-ping', ?, ?, ?)""",
            (
                "ok" if websub_result.get("ok") or bing_result.get("ok") else "error",
                f"websub={websub_result.get('detail')}; bing={bing_result.get('detail')}"[:500],
                utcnow(),
            ),
        )
    return {"websub": websub_result, "bing": bing_result}


async def run_queue(
    *,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
) -> dict[str, Any]:
    """Process queued links up to each running project's remaining daily speed."""
    release_stuck()
    started = time.monotonic()
    deadline = started + budget_seconds()
    summary: dict[str, Any] = {
        "processed": 0,
        "failed": 0,
        "projects": 0,
        "deferred": 0,
        "stopped_for_time": False,
    }
    owned = _owned_hosts()
    robots = RobotsCache()
    ping: Optional[dict] = None
    touched_users: dict[int, int] = {}
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
        limits=httpx.Limits(max_connections=FETCH_CONCURRENCY + 4, max_keepalive_connections=8),
    ) as client:
        for project in _due_projects(user_id, project_id):
            room = allowance(project)
            rows = _queued_rows(project["id"], room)
            if not rows:
                continue
            summary["projects"] += 1
            for row in rows:
                if time.monotonic() >= deadline:
                    summary["stopped_for_time"] = True
                    summary["deferred"] += 1
                    break
                outcome = await _process_row(client, row, project, owned, robots)
                if outcome == "failed":
                    summary["failed"] += 1
                    summary["processed"] += 1
                elif outcome == "sent":
                    summary["processed"] += 1
                touched_users[project["user_id"]] = touched_users.get(project["user_id"], 0) + (
                    1 if outcome in {"sent", "failed"} else 0
                )
            if summary["stopped_for_time"]:
                break
        if summary["processed"] and user_id is None and project_id is None:
            ping = await _maybe_ping(client, _public_base())
    for owner_id, count in touched_users.items():
        if not count:
            continue
        extra = ""
        if summary["stopped_for_time"]:
            extra = " The time budget ran out; run Process due now to continue today's quota."
        _notify(
            owner_id,
            f"Queue processed {count} link(s). Indexed counts change only after a SERP check or a manual mark.{extra}",
        )
    summary["ping"] = ping
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return summary


def mark_engines(user_id: int, project_id: int, ids: list[int], engine: str, indexed: bool) -> int:
    if engine not in ENGINES or not ids:
        return 0
    column = f"{engine}_state"
    state = "indexed" if indexed else "not_indexed"
    now = utcnow()
    status = _index_status(state if engine == "google" else "unknown", "manual")
    # Manual marks on Yandex/Bing must not pretend the Google check changed.
    with tx() as conn:
        owned = conn.execute(
            "SELECT id FROM projects WHERE id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
        if not owned:
            return 0
        placeholders = ",".join("?" * len(ids))
        if engine == "google":
            cur = conn.execute(
                f"""UPDATE urls SET {column}=?, index_status=?, index_source='manual',
                    index_note='Manually marked by user.', last_index_check_at=?, updated_at=?
                    WHERE project_id=? AND id IN ({placeholders})""",
                [state, "manual_indexed" if indexed else "manual_not_indexed", now, now, project_id, *ids],
            )
        else:
            cur = conn.execute(
                f"""UPDATE urls SET {column}=?, index_source='manual',
                    index_note='Manually marked by user.', updated_at=?
                    WHERE project_id=? AND id IN ({placeholders})""",
                [state, now, project_id, *ids],
            )
        return cur.rowcount


async def check_ids(user_id: int, project_id: int, ids: list[int]) -> tuple[int, str]:
    """Run the configured SERP check. Does not invent a status when the check fails."""
    if not ids:
        return 0, "Select at least one link."
    settings = get_settings()
    provider = settings.get("serp_provider") or "serpapi"
    api_key = settings.get("serp_api_key") or ""
    if not api_key and provider != "custom":
        return 0, "No SERP API key is configured. Index status was left unchanged."
    rows = get_conn().execute(
        f"""SELECT id, url FROM urls
            WHERE project_id = ? AND id IN ({",".join("?" * len(ids))})
              AND project_id IN (SELECT id FROM projects WHERE id = ? AND user_id = ?)""",
        (project_id, *ids, project_id, user_id),
    ).fetchall()
    if not rows:
        return 0, "No matching links."
    updated = 0
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
        for row in rows:
            result = await serp.check_indexed(
                client, row["url"], provider, api_key, settings.get("serp_endpoint") or ""
            )
            if not result.get("ok") or result.get("indexed") is None:
                continue
            state = "indexed" if result["indexed"] else "not_indexed"
            note = result.get("detail") or "Google SERP check finished."
            hits = result.get("hits") or []
            if hits:
                note += " Hits: " + ", ".join(hits[:3])
            now = utcnow()
            with tx() as conn:
                conn.execute(
                    """UPDATE urls SET google_state=?, index_status=?, index_source=?, index_note=?,
                       last_index_check_at=?, updated_at=? WHERE id=?""",
                    (
                        state,
                        _index_status(state, provider),
                        provider,
                        note[:500],
                        now,
                        now,
                        row["id"],
                    ),
                )
            updated += 1
    if updated == 0:
        return 0, "The SERP check did not return an index status. Nothing was changed."
    return updated, ""


def requeue(user_id: int, project_id: int, ids: list[int]) -> int:
    if not ids:
        return 0
    with tx() as conn:
        owned = conn.execute(
            "SELECT id FROM projects WHERE id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
        if not owned:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"""UPDATE urls SET queue_status='queued', updated_at=?
                WHERE project_id=? AND id IN ({placeholders})""",
            [utcnow(), project_id, *ids],
        )
        return cur.rowcount
