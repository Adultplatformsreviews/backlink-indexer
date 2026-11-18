"""Indexer network, credits, SERP confirmation, and API extensions."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backlink_indexer.auth import rotate_api_key
from backlink_indexer.credits import confirm_indexed, refund_expired, summary_for_user
from backlink_indexer.database import close_conn, get_conn, set_setting, tx, utcnow
from backlink_indexer.engine import (
    GOOGLE_PUSH_WARNING,
    apply_serp,
    precheck_ids,
    run_drip,
    user_status_label,
)
from backlink_indexer.network import googlebot_stats, verify_crawler
from backlink_indexer.services.crypto import decrypt_text, encrypt_text
from backlink_indexer.services.google_indexing import publish_notification
from backlink_indexer.services.indexnow import submit
from backlink_indexer.services.urlutil import canonicalize
from tests.test_app import signup


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backlink_indexer.main import app

    with TestClient(app) as test_client:
        conn = get_conn()
        for table in (
            "cron_leases", "events", "serp_cache", "hub_pages", "hub_domains", "connections",
            "submission_log", "urls", "batches", "notifications", "tickets",
            "api_keys", "sessions", "projects", "owned_hosts", "daily_usage",
            "login_codes", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        close_conn()
        yield test_client


def test_canonicalize_strips_tracking_fragment_and_lowercases_host():
    assert canonicalize("  HTTPS://Example.COM/Path?utm_source=a&fbclid=1&gclid=2&keep=1#section ") == (
        "https://example.com/Path?keep=1"
    )
    assert canonicalize("https://example.com/a") == canonicalize("https://example.com/a?utm_medium=x")


def test_api_dedupes_and_reports_skipped_duplicates(client):
    signup(client, "apiuser", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='apiuser'").fetchone()["id"]
    close_conn()
    key = rotate_api_key(user_id)
    created = client.post(
        "/api/v1/campaigns",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "name": "Demo",
            "project_type": "indexing",
            "daily_speed": 10,
            "urls": [
                "https://Example.com/a?utm_source=newsletter",
                "https://example.com/a",
                "https://example.com/b",
            ],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert "project" in body
    assert body["skipped_duplicates"]
    assert len(body["accepted"]) == 2
    assert body["not_sendable"] == []
    project_id = body["project"]["id"]
    detail = client.get(f"/api/v1/campaigns/{project_id}", headers={"Authorization": f"Bearer {key}"})
    report = detail.json()
    assert report["indexed_rate"] == 0
    assert report["avg_days_to_index"] is None
    assert "by_status" in report["counts"]
    assert "refunded" in report
    rows = client.get(
        f"/api/v1/campaigns/{project_id}/urls?status=queued",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert rows.status_code == 200
    assert rows.json()["total"] == 2
    assert rows.json()["urls"][0]["status"] == "queued"


def test_precheck_refunds_noindex_and_http_errors(client, monkeypatch):
    monkeypatch.setenv("INDEXER_HOST_INTERVAL", "0")
    signup(client, "pre", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='pre'").fetchone()["id"]
    before = get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"]
    close_conn()
    created = client.post(
        "/app/projects",
        data={
            "name": "Precheck",
            "project_type": "indexing",
            "daily_speed": "5",
            "urls": "https://example.com/noindex\nhttps://example.com/missing\nhttps://example.com/ok",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("robots.txt"):
            return httpx.Response(404, text="")
        if path.endswith("/noindex"):
            html = "<html><head><meta name='robots' content='noindex'></head><body></body></html>"
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if path.endswith("/missing"):
            return httpx.Response(404, text="missing")
        html = "<html><head><title>ok</title></head><body>live</body></html>"
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    ids = [int(row["id"]) for row in get_conn().execute("SELECT id FROM urls ORDER BY id").fetchall()]
    close_conn()

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as http:
            return await precheck_ids(ids, http)

    import asyncio

    outcome = asyncio.run(run())
    reasons = {item["url"]: item["reason"] for item in outcome["not_sendable"]}
    assert "noindex" in reasons["https://example.com/noindex"]
    assert "404" in reasons["https://example.com/missing"]
    assert any(item["url"].endswith("/ok") for item in outcome["accepted"])
    balance = get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"]
    close_conn()
    # Three credits were reserved. Two not-sendable rows were released.
    assert balance == before - 1
    page = client.get(created.headers["location"])
    assert "In Queue" in page.text
    assert "Not indexable (refunded)" in page.text
    assert "External pages can't be forced into Google's index." in page.text
    assert "Reports count confirmed indexed URLs only." in page.text


def test_publish_notification_and_indexnow_are_mockable():
    def handler(request: httpx.Request) -> httpx.Response:
        if "indexing.googleapis.com" in str(request.url):
            body = json.loads(request.content.decode())
            assert body["type"] == "URL_UPDATED"
            assert request.headers["authorization"] == "Bearer test-token"
            return httpx.Response(200, json={"urlNotificationMetadata": {"url": body["url"]}})
        assert request.headers["content-type"].startswith("application/json")
        return httpx.Response(200, text="ok")

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            google = await publish_notification(http, "test-token", "https://links.example/l/abcd")
            ping = await submit(
                http,
                ["https://links.example/l/abcd"],
                "abc12345",
                "https://links.example/abc12345.txt",
            )
            return google, ping

    import asyncio

    google, ping = asyncio.run(run())
    assert google["ok"] is True
    assert ping["state"] == "accepted"
    assert ping["state"] != "indexed"


def test_drip_rotates_domains_and_skips_google_unless_enabled(client, monkeypatch):
    monkeypatch.setattr("backlink_indexer.network.PAGE_URLS", 1)
    monkeypatch.setattr("backlink_indexer.engine.network.PAGE_URLS", 1)
    signup(client, "operator", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    created = client.post(
        "/app/projects",
        data={
            "name": "Drip",
            "project_type": "indexing",
            "daily_speed": "40",
            "urls": "\n".join(f"https://other.example/p{i}" for i in range(2)),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    now = utcnow()
    with tx() as conn:
        conn.execute(
            """UPDATE urls SET kind='external', status='queued', precheck_json='{"http_status":200}',
               normalized_url=url, host='other.example'"""
        )
    close_conn()
    from backlink_indexer import network

    first, error = network.add_domain("links-a.example", google_push=False)
    assert not error
    second, error = network.add_domain("links-b.example", google_push=True)
    assert not error
    with tx() as conn:
        conn.execute(
            "UPDATE hub_domains SET indexnow_verified_at=? WHERE id IN (?, ?)",
            (now, first["id"], second["id"]),
        )
    info = {
        "type": "service_account",
        "private_key": "test",
        "client_email": "bot@example.iam.gserviceaccount.com",
        "_access_token": "ya29-test",
    }
    token = encrypt_text(json.dumps([info]))
    with tx() as conn:
        conn.execute(
            "UPDATE hub_domains SET gcp_projects_json_encrypted=? WHERE id=?",
            (token, second["id"]),
        )
    close_conn()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "indexing.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text="ok")

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            return await run_drip(http)

    import asyncio

    asyncio.run(run())
    pages = get_conn().execute("SELECT hub_domain_id, pushes_count FROM hub_pages ORDER BY id").fetchall()
    close_conn()
    assert len(pages) == 2
    assert {row["hub_domain_id"] for row in pages} == {first["id"], second["id"]}
    assert all(row["pushes_count"] == 1 for row in pages)
    assert any("indexing.googleapis.com" in url for url in seen)
    # The domain with Google push off must not be the only caller; exactly one Google call
    # because only one page sits on the enabled domain.
    assert sum("indexing.googleapis.com" in url for url in seen) == 1
    page = client.get("/app/settings/network")
    assert page.status_code == 200
    assert GOOGLE_PUSH_WARNING in page.text
    assert "links-a.example" in page.text


def test_serp_confirmation_cache_and_refund_is_not_recharged(client, monkeypatch):
    signup(client, "serp", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='serp'").fetchone()["id"]
    close_conn()
    client.post(
        "/app/projects",
        data={"name": "Serp", "project_type": "indexing", "daily_speed": "5", "urls": "https://example.com/target"},
        follow_redirects=False,
    )
    url_id = int(get_conn().execute("SELECT id FROM urls").fetchone()["id"])
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tx() as conn:
        conn.execute(
            "UPDATE urls SET created_at=?, credits_state='reserved', credits_held=1, status='submitted' WHERE id=?",
            (old, url_id),
        )
        conn.execute("UPDATE users SET token_balance=10 WHERE id=?", (user_id,))
    close_conn()
    assert refund_expired() == 1
    balance = get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"]
    state = get_conn().execute("SELECT credits_state, status FROM urls WHERE id=?", (url_id,)).fetchone()
    close_conn()
    assert balance == 11
    assert state["credits_state"] == "refunded"
    assert state["status"] == "submitted"
    assert user_status_label(dict(state)) == "Refunded"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        query = request.url.params.get("q", "")
        link = "https://example.com/target" if "site:" in query else "https://example.com/other"
        if request.url.params.get("engine") == "yandex":
            return httpx.Response(200, json={"error": "Yandex engine is not available on this plan."})
        return httpx.Response(200, json={"organic_results": [{"link": link}]})

    async def run():
        from backlink_indexer.engine import check_url_serps

        set_setting("serp_provider", "serpapi")
        set_setting("serp_api_key", "serp-test-key")
        set_setting("serp_daily_budget", "10")
        set_setting("serp_budget_date", "2000-01-01")
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            first = await check_url_serps(http, "https://example.com/target")
            second = await check_url_serps(http, "https://example.com/target")
            return first, second

    import asyncio

    first, second = asyncio.run(run())
    assert first["google"]["indexed"] is True
    assert first["yandex"]["unverified"] is True
    assert second["google"].get("cached") is True
    apply_serp(url_id, first)
    row = get_conn().execute(
        "SELECT status, google_state, yandex_state, credits_state FROM urls WHERE id=?",
        (url_id,),
    ).fetchone()
    balance_after = get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"]
    close_conn()
    assert row["google_state"] == "indexed"
    assert row["status"] == "indexed"
    assert row["yandex_state"] == "unverified"
    assert row["credits_state"] == "refunded"
    assert balance_after == 11
    confirm_indexed(url_id)
    assert get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"] == 11
    close_conn()
    # site: matched, so the inurl fallback was not required. Bing still searches. Yandex errors are not cached.
    assert calls["n"] >= 2


def test_recheck_is_limited_to_once_per_day(client, monkeypatch):
    signup(client, "recheck", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='recheck'").fetchone()["id"]
    close_conn()
    key = rotate_api_key(user_id)
    client.post(
        "/app/projects",
        data={"name": "Recheck", "project_type": "indexing", "daily_speed": "4", "urls": "https://example.com/z"},
        follow_redirects=False,
    )
    url_id = int(get_conn().execute("SELECT id FROM urls").fetchone()["id"])
    with tx() as conn:
        conn.execute("UPDATE urls SET last_check_at=? WHERE id=?", (utcnow(), url_id))
    close_conn()
    blocked = client.post(f"/api/v1/urls/{url_id}/recheck", headers={"Authorization": f"Bearer {key}"})
    assert blocked.status_code == 429

    async def fake(client_http, url):
        return {
            "google": {"ok": True, "indexed": False, "detail": "Not found in top results."},
            "bing": {"ok": True, "indexed": False, "detail": "Not found in top results."},
            "yandex": {"ok": False, "indexed": None, "unverified": True, "detail": "unavailable"},
        }

    monkeypatch.setattr("backlink_indexer.engine.check_url_serps", fake)
    with tx() as conn:
        conn.execute("UPDATE urls SET last_check_at='' WHERE id=?", (url_id,))
    close_conn()
    allowed = client.post(f"/api/v1/urls/{url_id}/recheck", headers={"Authorization": f"Bearer {key}"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["google_state"] == "not_indexed"
    assert allowed.json()["status"] != "indexed"


def test_cron_routes_require_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    for path in (
        "/api/cron/precheck",
        "/api/cron/drip",
        "/api/cron/serp-check",
        "/api/cron/inspection-poll",
        "/api/cron/quota-reset",
    ):
        assert client.get(path).status_code == 401
    ok = client.post("/api/cron/quota-reset", headers={"Authorization": "Bearer cron-secret"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_fallback_hub_is_paged_nofollow_and_sitemap_is_local(client):
    signup(client, "hubpage", "password1")
    urls = "\n".join(f"https://example.com/listed{i}" for i in range(3))
    client.post(
        "/app/projects",
        data={"name": "Listed", "project_type": "indexing", "daily_speed": "5", "urls": urls},
        follow_redirects=False,
    )
    first = client.get("/hub?page=1")
    assert first.status_code == 200
    assert 'rel="nofollow ugc"' in first.text
    assert "page 1 of 1" in first.text
    sitemap = client.get("/hub/sitemap.xml")
    assert "https://example.com/listed0" not in sitemap.text
    assert "/hub" in sitemap.text
    home = client.get("/")
    assert "Own-site submission via Search Console + IndexNow." in home.text
    assert "External link discovery through our indexer network, SERP-verified." in home.text
    assert "Googlebot visits today" not in home.text
    credits = client.get("/api/v1/credits", headers={"Authorization": "Bearer nope"})
    assert credits.status_code == 401


def test_googlebot_stats_use_verified_hits_only(monkeypatch):
    monkeypatch.setattr("backlink_indexer.network.socket.gethostbyaddr", lambda ip: ("crawl.googlebot.com", [], [ip]))
    monkeypatch.setattr("backlink_indexer.network._forward_matches", lambda host, ip: ip == "66.249.66.1")
    assert verify_crawler("66.249.66.1", "google") is True
    assert verify_crawler("203.0.113.8", "google") is False
    assert googlebot_stats(None) is None


def test_measured_googlebot_timing_is_shown(client):
    signup(client, "stats", "password1")
    client.post(
        "/app/projects",
        data={"name": "Stats", "project_type": "indexing", "daily_speed": "5", "urls": "https://example.com/s"},
        follow_redirects=False,
    )
    url_id = int(get_conn().execute("SELECT id, project_id FROM urls").fetchone()["id"])
    project_id = int(get_conn().execute("SELECT project_id FROM urls").fetchone()["project_id"])
    pushed = utcnow()
    # A visit 30 minutes after the push, still today.
    visited = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Keep the visit on the same calendar day as utcnow by using a push 20 minutes ago
    # when that still falls on today; otherwise skip the clock edge by clamping.
    start = datetime.now(timezone.utc) - timedelta(minutes=20)
    end = datetime.now(timezone.utc) - timedelta(minutes=5)
    if start.date() != end.date():
        start = end - timedelta(minutes=5)
    pushed = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    visited = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    hits = json.dumps({"first_push_at": pushed, "google": {"count": 1, "first_at": visited, "verified": True}})
    with tx() as conn:
        page_id = conn.execute(
            """INSERT INTO hub_pages
               (hub_domain_id, slug, url_ids_json, intro_text, pushes_count, last_pushed_at, bot_hits_json, created_at)
               VALUES (NULL, 'abcd', ?, 'Intro sentence one. Intro sentence two. Intro sentence three.', 1, ?, ?, ?)""",
            (json.dumps([url_id]), pushed, hits, pushed),
        )
        # sqlite lastrowid via a follow-up select
    close_conn()
    stats = googlebot_stats(project_id)
    assert stats is not None
    assert stats["tracked"] == 1
    assert stats["min_seconds"] > 0
    page = client.get(f"/app/projects/{project_id}")
    assert "Verified Googlebot visits today" in page.text
    assert "URLs tracked" in page.text
    home = client.get("/")
    assert "Verified Googlebot visits today" in home.text


def test_index_checker_uses_budget_and_credits(client, monkeypatch):
    signup(client, "check", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='check'").fetchone()["id"]
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance=5 WHERE id=?", (user_id,))
    close_conn()
    set_setting("checker_tokens_per_url", "2")

    async def fake(client_http, url):
        return {
            "google": {"ok": True, "indexed": True, "detail": "Indexed for site:."},
            "bing": {"ok": True, "indexed": False, "detail": "Not found in top results."},
            "yandex": {"ok": False, "indexed": None, "unverified": True, "detail": "unavailable"},
        }

    monkeypatch.setattr("backlink_indexer.engine.check_url_serps", fake)
    page = client.post("/app/tools/check", data={"urls": "https://example.com/checked"}, follow_redirects=True)
    assert page.status_code == 200
    assert "indexed" in page.text
    assert "not_found" in page.text
    balance = get_conn().execute("SELECT token_balance FROM users WHERE id=?", (user_id,)).fetchone()["token_balance"]
    close_conn()
    assert balance == 3


def test_credits_endpoint_includes_refunded_counts(client):
    signup(client, "bal", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='bal'").fetchone()["id"]
    close_conn()
    key = rotate_api_key(user_id)
    body = client.get("/api/v1/credits", headers={"Authorization": f"Bearer {key}"}).json()
    assert "token_balance" in body
    assert body["refunded_urls"] == 0
    assert "reserved_credits" in body
    summary = summary_for_user(user_id)
    assert summary["refunded_urls"] == 0


def test_mcp_lists_tools_and_reads_credits(client):
    signup(client, "mcp", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='mcp'").fetchone()["id"]
    close_conn()
    key = rotate_api_key(user_id)
    listed = client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {key}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert names == ["get_credits", "create_campaign", "get_campaign", "list_urls"]
    credits = client.post(
        "/api/mcp",
        headers={"Authorization": f"Bearer {key}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_credits", "arguments": {}}},
    )
    assert credits.status_code == 200
    assert credits.json()["result"]["structuredContent"]["refunded_urls"] == 0


def test_vercel_crons_run_at_most_once_per_day():
    import json
    from pathlib import Path

    data = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    schedules = [item["schedule"] for item in data["crons"]]
    assert schedules
    for schedule in schedules:
        minute, hour, day, month, weekday = schedule.split()
        assert minute.isdigit() and hour.isdigit()
        assert "/" not in schedule
        assert day == month == weekday == "*"


def test_lease_blocks_overlap_until_it_expires():
    from backlink_indexer.cronlock import acquire_lease, release_lease

    owner = acquire_lease("precheck", 60)
    assert owner
    assert acquire_lease("precheck", 60) is None
    with tx() as conn:
        conn.execute(
            "UPDATE cron_leases SET expires_at = ? WHERE name = ?",
            ("2000-01-01T00:00:00Z", "precheck"),
        )
    close_conn()
    renewed = acquire_lease("precheck", 30)
    assert renewed
    release_lease("precheck", renewed)
    assert acquire_lease("precheck", 30)


def test_overlapping_precheck_returns_ok_without_running(client, monkeypatch):
    from backlink_indexer.cronlock import acquire_lease, release_lease

    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    owner = acquire_lease("precheck", 60)
    assert owner
    close_conn()
    response = client.post("/api/cron/precheck", headers={"Authorization": "Bearer cron-secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["skipped"] is True
    release_lease("precheck", owner)


def test_roundtrip_encryption_does_not_store_plaintext():
    token = encrypt_text('{"private_key":"secret-key"}')
    assert "secret-key" not in token
    assert "secret-key" in decrypt_text(token)


def _queue_external(conn, url: str, normalized: str) -> None:
    conn.execute(
        """UPDATE urls
           SET kind = 'external', status = 'queued', precheck_json = ?,
               normalized_url = ?, host = 'www.branle-entre-potes.com', hub_page_id = NULL
           WHERE url = ?""",
        ('{"http_status": 200}', normalized, url),
    )


def test_drip_publishes_prechecked_external_without_hub_domain(client, monkeypatch):
    """No hub domain and no IndexNow key still publishes the fallback /l/<slug> page."""
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    set_setting("indexnow_key", "")
    signup(client, "fallback", "password1")
    original = "https://www.branle-entre-potes.com/forum/viewtopic.php?f=5&t=2916&p=89166"
    normalized = "https://www.branle-entre-potes.com/forums/sujet/1210?page=1"
    created = client.post(
        "/app/projects",
        data={"name": "Project 3", "project_type": "indexing", "daily_speed": "10", "urls": original},
        follow_redirects=False,
    )
    assert created.status_code == 303
    empty = client.post("/api/cron/drip", headers={"Authorization": "Bearer cron-secret"})
    assert empty.status_code == 200
    assert empty.json()["pages"] == 0
    assert empty.json()["ok"] is True
    with tx() as conn:
        _queue_external(conn, original, normalized)
    close_conn()
    response = client.post("/api/cron/drip", headers={"Authorization": "Bearer cron-secret"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["ok"] is True
    assert body["pages"] == 1
    assert body["errors"] == []
    row = get_conn().execute(
        "SELECT id, status, hub_page_id FROM urls WHERE url = ?",
        (original,),
    ).fetchone()
    page = get_conn().execute(
        "SELECT slug, hub_domain_id FROM hub_pages WHERE id = ?",
        (row["hub_page_id"],),
    ).fetchone()
    event = get_conn().execute(
        "SELECT payload_json FROM events WHERE type = 'indexnow_push' AND url_id = ?",
        (row["id"],),
    ).fetchone()
    close_conn()
    assert row["status"] == "submitted"
    assert page["hub_domain_id"] is None
    assert page["slug"]
    assert "Fallback hub has no IndexNow key." in event["payload_json"]
    shown = client.get(f"/l/{page['slug']}")
    assert shown.status_code == 200
    assert "viewtopic.php?f=5&amp;t=2916&amp;p=89166" in shown.text


def test_fallback_slug_skips_a_page_that_already_uses_it(client, monkeypatch):
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    set_setting("indexnow_key", "")
    signup(client, "slug", "password1")
    original = "https://www.branle-entre-potes.com/forum/viewtopic.php?f=5&t=2916&p=89166"
    client.post(
        "/app/projects",
        data={"name": "Slug", "project_type": "indexing", "daily_speed": "5", "urls": original},
        follow_redirects=False,
    )
    with tx() as conn:
        _queue_external(conn, original, "https://www.branle-entre-potes.com/forums/sujet/1210?page=1")
        conn.execute(
            """INSERT INTO hub_pages
               (hub_domain_id, slug, url_ids_json, intro_text, pushes_count, last_pushed_at, bot_hits_json, created_at)
               VALUES (NULL, 'aaaaaaaa', '[]', 'Existing page.', 0, '', '{}', ?)""",
            (utcnow(),),
        )
    close_conn()
    slugs = iter(("aaaaaaaa", "bbbbbbbb"))
    monkeypatch.setattr("backlink_indexer.network.secrets.token_hex", lambda n=16: next(slugs))

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
        async with httpx.AsyncClient(transport=transport) as http:
            return await run_drip(http)

    import asyncio

    result = asyncio.run(run())
    assert result["errors"] == []
    assert result["pages"] == 1
    page = get_conn().execute(
        "SELECT slug FROM hub_pages WHERE slug != 'aaaaaaaa'"
    ).fetchone()
    close_conn()
    assert page["slug"] == "bbbbbbbb"


def test_cron_drip_returns_json_when_one_url_fails(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setattr("backlink_indexer.engine.network.PAGE_URLS", 1)
    signup(client, "partial", "password1")
    first = "https://example.com/first"
    second = "https://example.com/second"
    client.post(
        "/app/projects",
        data={
            "name": "Partial",
            "project_type": "indexing",
            "daily_speed": "10",
            "urls": f"{first}\n{second}",
        },
        follow_redirects=False,
    )
    with tx() as conn:
        conn.execute(
            """UPDATE urls SET kind='external', status='queued',
               precheck_json='{"http_status":200}', normalized_url=url, host='example.com'"""
        )
    close_conn()
    from backlink_indexer import network

    real_create = network.create_page

    def flaky(domain, url_ids):
        if url_ids and url_ids[0] == int(get_conn().execute("SELECT MIN(id) AS id FROM urls").fetchone()["id"]):
            raise RuntimeError("hub insert failed")
        return real_create(domain, url_ids)

    monkeypatch.setattr("backlink_indexer.engine.network.create_page", flaky)
    response = client.post("/api/cron/drip", headers={"Authorization": "Bearer cron-secret"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.text != "Internal Server Error"
    body = response.json()
    assert body["ok"] is False
    assert body["pages"] == 1
    assert body["errors"][0]["error"].startswith("RuntimeError: hub insert failed")
    rows = {
        row["url"]: row["status"]
        for row in get_conn().execute("SELECT url, status FROM urls").fetchall()
    }
    close_conn()
    assert rows[first] == "queued"
    assert rows[second] == "submitted"


def test_cron_drip_returns_json_when_the_runner_raises(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")

    async def boom(client=None):
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr("backlink_indexer.indexer_routes.run_drip", boom)
    response = client.post("/api/cron/drip", headers={"Authorization": "Bearer cron-secret"})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "RuntimeError: scheduler blew up"
    assert body["errors"][0]["error"] == "RuntimeError: scheduler blew up"
