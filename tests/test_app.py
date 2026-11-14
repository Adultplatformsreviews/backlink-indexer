import re

import pytest
from fastapi.testclient import TestClient

from backlink_indexer.database import close_conn, get_conn, get_settings, resolve_database_url, set_setting, tx, utcnow
from backlink_indexer.limits import record_added
from backlink_indexer.main import app


@pytest.fixture
def client():
    if (resolve_database_url()[0] or "").startswith("postgres"):
        raise RuntimeError("Refusing to wipe a Postgres database from the test suite.")
    with TestClient(app) as test_client:
        conn = get_conn()
        for table in (
            "submission_log",
            "urls",
            "batches",
            "notifications",
            "tickets",
            "api_keys",
            "sessions",
            "projects",
            "owned_hosts",
            "daily_usage",
            "login_codes",
            "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        close_conn()
        yield test_client


def signup(client, username="ada", password="password1", email=None):
    response = client.post(
        "/signup",
        data={"username": username, "email": email or f"{username}@example.com", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def test_database_url_fallbacks(monkeypatch):
    for key in (
        "DATABASE_URL",
        "POSTGRES_URL",
        "POSTGRES_PRISMA_URL",
        "DATABASE_URL_UNPOOLED",
        "POSTGRES_URL_NON_POOLING",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://user:pass@ep.neon.tech/neondb?sslmode=require")
    url, name = resolve_database_url()
    assert name == "POSTGRES_URL"
    assert url.startswith("postgresql://")
    monkeypatch.setenv("DATABASE_URL", "postgres://primary/db")
    url, name = resolve_database_url()
    assert name == "DATABASE_URL"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://direct/db")
    url, name = resolve_database_url()
    assert name == "DATABASE_URL_UNPOOLED"


def test_health_sqlite_is_ephemeral(client):
    body = client.get("/health").json()
    assert body["persistent_db"] is False
    assert body["ephemeral_storage"] is True
    assert body["database_env"] == ""


def test_assistant_is_gone(client):
    response = client.get("/app/assistant", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app"
    page = client.get("/app/assistant")
    assert "command mode" not in page.text.lower()
    assert "Clear chat" not in page.text
    assert client.get("/api/assistant/chat").status_code == 404


def test_signup_project_tokens_and_daily_speed(client, monkeypatch):
    signup(client)
    page = client.get("/app")
    assert "Data is not persistent" in page.text
    assert "Add project" in page.text
    assert "2index" not in page.text.lower()
    created = client.post(
        "/app/projects",
        data={
            "name": "spicyranked",
            "project_type": "indexing",
            "target_domain": "example.com",
            "daily_speed": "2",
            "urls": "\n".join(f"https://example.com/p{i}" for i in range(5)),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail = client.get(created.headers["location"])
    assert "spicyranked" in detail.text
    assert "Unknown" in detail.text
    credits = client.get("/app/plans")
    assert "5 of 100 used today" in credits.text

    async def fake_precheck(http, url, target, include, cache):
        return {
            "final_url": url,
            "http_status": 200,
            "is_live": True,
            "fetch_error": None,
            "content_type": "text/html",
            "meta_robots": "",
            "x_robots_tag": "",
            "noindex": False,
            "robots_disallow": False,
            "canonical": url,
            "canonicalized_away": False,
            "has_link": True,
            "link_rel": "dofollow",
            "link_count": 1,
            "link_details_json": "[]",
        }

    monkeypatch.setattr("backlink_indexer.queue.precheck_url", fake_precheck)
    project_path = created.headers["location"].split("?")[0]
    first = client.post(project_path + "/process", follow_redirects=False)
    assert first.status_code == 303
    rows = get_conn().execute("SELECT queue_status, google_state FROM urls ORDER BY id").fetchall()
    close_conn()
    assert sum(row["queue_status"] == "sent" for row in rows) == 2
    assert sum(row["queue_status"] == "queued" for row in rows) == 3
    assert all(row["google_state"] == "external_hub" for row in rows if row["queue_status"] == "sent")
    second = client.post(project_path + "/process", follow_redirects=True)
    assert "Nothing was due" in second.text


def test_indexnow_submission_is_not_indexed(client, monkeypatch):
    signup(client, "owner", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    client.post(
        "/app/settings",
        data={"action": "add-host", "host": "example.com"},
        follow_redirects=False,
    )
    client.post(
        "/app/settings",
        data={"action": "save", "indexnow_key": "abcd1234ef", "tokens_per_url": "1",
              "public_base_url": "", "default_target_domain": "example.com",
              "websub_hub": "https://pubsubhubbub.appspot.com/", "hub_include": "all",
              "serp_provider": "serpapi"},
        follow_redirects=False,
    )
    created = client.post(
        "/app/projects",
        data={
            "name": "Owned",
            "project_type": "instant",
            "target_domain": "example.com",
            "daily_speed": "5",
            "urls": "https://example.com/owned",
        },
        follow_redirects=False,
    )

    async def fake_precheck(http, url, target, include, cache):
        return {"final_url": url, "http_status": 200, "is_live": True, "fetch_error": None,
                "content_type": "text/html", "meta_robots": "", "x_robots_tag": "", "noindex": False,
                "robots_disallow": False, "canonical": url, "canonicalized_away": False,
                "has_link": False, "link_rel": "none", "link_count": 0, "link_details_json": "[]"}

    async def fake_submit(http, urls, key, key_location, endpoint=None):
        return {"ok": True, "detail": "HTTP 200", "count": 1, "state": "accepted", "endpoint": endpoint}

    monkeypatch.setattr("backlink_indexer.queue.precheck_url", fake_precheck)
    monkeypatch.setattr("backlink_indexer.queue.indexnow.submit", fake_submit)
    monkeypatch.setattr(
        "backlink_indexer.queue.google_indexing.publish_url",
        lambda url: {"ok": False, "detail": "No service account."},
    )
    monkeypatch.setattr("backlink_indexer.queue.google_indexing.service_account_present", lambda: False)
    path = created.headers["location"].split("?")[0]
    client.post(path + "/process", follow_redirects=True)
    row = get_conn().execute(
        "SELECT google_state, yandex_state, bing_state, queue_status FROM urls"
    ).fetchone()
    close_conn()
    assert row["queue_status"] == "sent"
    assert row["yandex_state"] == "accepted"
    assert row["bing_state"] == "accepted"
    assert row["google_state"] == "not_configured"


def test_manual_mark_and_pagination(client):
    signup(client, "mia", "password1")
    created = client.post(
        "/app/projects",
        data={
            "name": "Pages",
            "project_type": "check",
            "target_domain": "example.com",
            "daily_speed": "10",
            "urls": "\n".join(f"https://example.com/c{i}" for i in range(12)),
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    everything = client.get(path)
    assert "c0" in everything.text and "c11" in everything.text
    assert "1 / 1" in everything.text
    assert ">All<" in everything.text
    queued = client.get(path + "?filter=queued")
    assert "c0" in queued.text and "c11" in queued.text
    page2 = client.get(path + "?per=10&page=2")
    assert "c0" in page2.text
    assert "c11" not in page2.text
    assert "2 / 2" in page2.text
    listed = client.get(path + "?per=all&q=c0")
    ids = re.findall(r'name="ids" value="(\d+)"', listed.text)
    assert ids
    done = client.post(path + "/bulk", data={"action": "mark_google_indexed", "ids": ids[0]}, follow_redirects=True)
    assert "Manually marked 1" in done.text
    again = client.get(path)
    assert "Indexed" in again.text
    assert "G 1" in client.get("/app").text or "G <b>1</b>" in client.get("/app").text


def test_archive_and_filters(client):
    signup(client, "sam", "password1")
    created = client.post(
        "/app/projects",
        data={"name": "To archive", "project_type": "check", "target_domain": "example.com", "daily_speed": "3", "urls": "https://example.com/z"},
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    client.post(path + "/archive", follow_redirects=False)
    assert "To archive" not in client.get("/app").text
    assert "To archive" in client.get("/app/archive").text
    assert "Indexing check" in client.get("/app?type=check").text or "No projects" in client.get("/app?type=check").text


def test_cron_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert client.get("/api/cron/tick").status_code == 401
    ok = client.get("/api/cron/tick", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_banner_hidden_when_database_configured(client, monkeypatch):
    signup(client, "neo", "password1")

    def report():
        return {
            "persistent_db": True,
            "ephemeral_storage": False,
            "database_env": "POSTGRES_URL",
            "database_error": "",
        }

    monkeypatch.setattr("backlink_indexer.ui.bootstrap_db", report)
    page = client.get("/app")
    assert "Data is not persistent" not in page.text
    assert "POSTGRES_URL" not in page.text or "persistent" in page.text.lower()


def test_username_password_admin_and_daily_cap(client):
    signup(client, "Ada", "password1", email="Ada@Example.com")
    stored = get_conn().execute("SELECT username, email, password_hash, is_admin FROM users").fetchone()
    close_conn()
    assert stored["username"] == "ada"
    assert stored["email"] == "ada@example.com"
    assert stored["is_admin"] == 0
    assert stored["password_hash"].startswith("scrypt$")
    assert "password1" not in stored["password_hash"]

    client.post("/logout")
    wrong = client.post("/login", data={"username": "ADA", "password": "nope-nope"}, follow_redirects=False)
    assert wrong.status_code == 400
    by_email = client.post(
        "/login",
        data={"username": "ADA@example.com", "password": "password1"},
        follow_redirects=False,
    )
    assert by_email.status_code == 303
    assert "Only the operator" in client.get("/app/users").text
    duplicate = client.post(
        "/signup",
        data={"username": "ada", "email": "other@example.com", "password": "password1"},
        follow_redirects=False,
    )
    assert duplicate.status_code == 400

    capped = client.post(
        "/app/projects",
        data={
            "name": "Cap",
            "project_type": "indexing",
            "target_domain": "example.com",
            "daily_speed": "10",
            "urls": "\n".join(f"https://example.com/u{i}" for i in range(101)),
        },
        follow_redirects=True,
    )
    assert "Daily limit" in capped.text
    count = get_conn().execute("SELECT COUNT(*) AS c FROM urls").fetchone()["c"]
    ada_id = get_conn().execute("SELECT id FROM users WHERE username = 'ada'").fetchone()["id"]
    close_conn()
    assert count == 100

    client.post("/logout")
    signup(client, "boss", "password1", email="MuhammadZeeshanHaiderOffical@gmail.com")
    assert "Unlimited" in client.get("/app/plans").text
    users = client.get("/app/users")
    assert "ada" in users.text
    assert "100 of 100" in users.text or "100 of 100" in users.text
    assert "VIP" in users.text
    client.post(f"/app/users/{ada_id}/password", data={"password": "newpass99"}, follow_redirects=False)
    client.post(f"/app/users/{ada_id}/disable", data={"disabled": "1"}, follow_redirects=False)
    boss = get_conn().execute("SELECT id FROM users WHERE username = 'boss'").fetchone()["id"]
    close_conn()
    blocked_op = client.post(f"/app/users/{boss}/disable", data={"disabled": "1"}, follow_redirects=True)
    assert "cannot be disabled" in blocked_op.text
    client.post("/logout")
    blocked = client.post("/login", data={"username": "ada", "password": "newpass99"}, follow_redirects=False)
    assert blocked.status_code == 400


def test_password_reset_requires_a_mailer_and_login_codes_are_absent(client, monkeypatch):
    for key in ("RESEND_API_KEY", "EMAIL_FROM", "SMTP_HOST", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    page = client.get("/reset")
    assert "administrator" in page.text.lower()
    posted = client.post("/reset", data={"email": "ada@example.com"})
    assert posted.status_code == 400
    assert client.get("/login/magic").status_code == 404
    assert client.get("/login/admin").status_code == 404
    assert client.post("/login/email").status_code == 404
    short = client.post(
        "/signup",
        data={"username": "tiny", "email": "tiny@example.com", "password": "short"},
        follow_redirects=False,
    )
    assert short.status_code == 400


def test_import_search_resubmit_history_and_chart(client, monkeypatch):
    signup(client, "nina", "password1")
    created = client.post(
        "/app/projects",
        data={"name": "Ledger", "project_type": "indexing", "target_domain": "example.com", "daily_speed": "5", "urls": ""},
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    pasted = client.post(
        path + "/urls",
        data={"urls": "https://example.com/keep\nhttps://example.com/keep\nnope"},
        follow_redirects=True,
    )
    assert "duplicate" in pasted.text.lower()
    assert "not URLs" in pasted.text
    uploaded = client.post(
        path + "/urls",
        data={"urls": ""},
        files={"upload": ("links.txt", b"https://example.com/from-txt\n", "text/plain")},
        follow_redirects=True,
    )
    assert "Added 1" in uploaded.text

    async def fake_sitemap(url):
        assert url.endswith("/sitemap.xml")
        return ["https://example.com/mapped", "https://example.com/keep"], ""

    monkeypatch.setattr("backlink_indexer.main._fetch_sitemap", fake_sitemap)
    mapped = client.post(
        path + "/urls",
        data={"sitemap_url": "https://example.com/sitemap.xml"},
        follow_redirects=True,
    )
    assert "Added 1" in mapped.text
    assert "duplicate" in mapped.text.lower()

    found = client.get(path + "?q=mapped")
    assert "example.com/mapped" in found.text
    assert "example.com/keep" not in found.text
    assert "Search" in found.text

    keep = get_conn().execute("SELECT id FROM urls WHERE url LIKE '%/keep'").fetchone()
    with tx() as conn:
        conn.execute(
            "UPDATE urls SET queue_status='failed', bing_state='rejected' WHERE id=?",
            (keep["id"],),
        )
        conn.execute(
            """INSERT INTO submission_log (url_id, method, status, detail, created_at)
               VALUES (?, 'bing', 'rejected', 'Bing Webmaster: HTTP 400', ?)""",
            (keep["id"], utcnow()),
        )
    close_conn()
    rejected = client.get(path + "?filter=rejected")
    assert "example.com/keep" in rejected.text
    resent = client.post(path + "/bulk", data={"action": "resubmit_failed"}, follow_redirects=True)
    assert "Re-queued 1" in resent.text
    status = get_conn().execute("SELECT queue_status, hold_until FROM urls WHERE id=?", (keep["id"],)).fetchone()
    close_conn()
    assert status["queue_status"] == "queued"
    assert status["hold_until"] == ""

    history = client.get(f"{path}/urls/{keep['id']}/history")
    assert history.status_code == 200
    body = history.json()
    assert body["url"].endswith("/keep")
    assert body["attempts"][0]["method"] == "bing"
    assert body["attempts"][0]["status"] == "rejected"
    assert "HTTP 400" in body["attempts"][0]["detail"]
    assert client.get(f"{path}/urls/999999/history").status_code == 404

    chart = client.get("/app?days=14")
    assert "Submissions per engine" in chart.text
    assert "Bing" in chart.text
    assert "Last 14 days" in chart.text
    assert "Last 30 days" in client.get("/app?days=30").text
    assert "Start free" in client.get("/").text


def test_public_pages_include_site_verification(client):
    home = client.get("/")
    assert home.status_code == 200
    assert home.headers.get("location") is None
    assert 'name="yandex-verification"' in home.text
    assert 'content="132993a7deb9e806"' in home.text
    assert "google-site-verification" not in home.text
    pricing = client.get("/pricing")
    assert 'name="yandex-verification"' in pricing.text
    signup(client, "verifier", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    settings = client.get("/app/settings")
    assert "Site verification" in settings.text
    assert "132993a7deb9e806" in settings.text
    stored = get_settings()
    close_conn()
    saved = client.post(
        "/app/settings",
        data={
            "action": "save",
            "public_base_url": stored.get("public_base_url", ""),
            "websub_hub": stored.get("websub_hub", ""),
            "hub_include": stored.get("hub_include") or "all",
            "indexnow_key": stored.get("indexnow_key", ""),
            "serp_provider": stored.get("serp_provider") or "serpapi",
            "serp_api_key": stored.get("serp_api_key", ""),
            "serp_endpoint": stored.get("serp_endpoint", ""),
            "tokens_per_url": stored.get("tokens_per_url") or "1",
            "bing_webmaster_api_key": stored.get("bing_webmaster_api_key", ""),
            "bing_site_url": stored.get("bing_site_url", ""),
            "yandex_webmaster_token": stored.get("yandex_webmaster_token", ""),
            "yandex_webmaster_user_id": stored.get("yandex_webmaster_user_id", ""),
            "yandex_webmaster_host_id": stored.get("yandex_webmaster_host_id", ""),
            "yandex_site_host": stored.get("yandex_site_host", ""),
            "yandex_verification": "abc123def456",
            "google_site_verification": "google-token_1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    client.post("/logout")
    home = client.get("/")
    assert 'content="abc123def456"' in home.text
    assert 'name="google-site-verification"' in home.text
    assert 'content="google-token_1"' in home.text
    set_setting("yandex_verification", "")
    set_setting("google_site_verification", "")
    close_conn()
    fallback = client.get("/")
    assert 'content="132993a7deb9e806"' in fallback.text
    assert "google-site-verification" not in fallback.text
    assert client.get("/health").json()["version"] == "1.4.2"


def test_import_respects_daily_limit(client):
    signup(client, "cap2", "password1")
    user_id = get_conn().execute("SELECT id FROM users WHERE username='cap2'").fetchone()["id"]
    close_conn()
    record_added(user_id, 99)
    created = client.post(
        "/app/projects",
        data={"name": "Cap", "project_type": "indexing", "target_domain": "example.com", "daily_speed": "4", "urls": ""},
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    added = client.post(
        path + "/urls",
        data={"urls": "https://example.com/a\nhttps://example.com/b"},
        follow_redirects=True,
    )
    assert "Daily limit" in added.text
    count = get_conn().execute("SELECT COUNT(*) AS c FROM urls").fetchone()["c"]
    close_conn()
    assert count == 1


def test_blank_email_operator_is_upgraded_on_login(client, monkeypatch):
    """Reproduce the live row: username is the operator local part and email is blank."""
    from backlink_indexer.auth import promote_operator_accounts

    signup(client, "muhammadzeeshanhaideroffical", "password1", email="other@example.com")
    with tx() as conn:
        conn.execute(
            """UPDATE users SET email = '', is_admin = 0, plan_name = 'Free'
               WHERE username = 'muhammadzeeshanhaideroffical'"""
        )
    close_conn()
    client.post("/logout")
    signed = client.post(
        "/login",
        data={"username": "MuhammadZeeshanHaiderOffical", "password": "password1"},
        follow_redirects=False,
    )
    assert signed.status_code == 303
    page = client.get("/app")
    assert "Unlimited" in page.text
    assert "0 of 100 used today" not in page.text
    assert "Free 100" not in page.text
    assert "left" not in page.text
    plans = client.get("/app/plans")
    assert "Engine quota left today" not in plans.text
    assert "10000 left" not in plans.text
    row = get_conn().execute(
        "SELECT email, is_admin, plan_name FROM users WHERE username = 'muhammadzeeshanhaideroffical'"
    ).fetchone()
    close_conn()
    assert row["email"] == "muhammadzeeshanhaideroffical@gmail.com"
    assert row["is_admin"] == 1
    assert row["plan_name"] == "VIP"

    from backlink_indexer.limits import is_unlimited_email

    monkeypatch.setenv("UNLIMITED_EMAILS", "   ")
    assert is_unlimited_email("muhammadzeeshanhaideroffical@gmail.com")
    monkeypatch.setenv("UNLIMITED_EMAILS", "other@example.com")
    assert not is_unlimited_email("muhammadzeeshanhaideroffical@gmail.com")
    monkeypatch.delenv("UNLIMITED_EMAILS", raising=False)
    assert promote_operator_accounts() == 0


def test_different_email_is_not_operator_even_if_username_matches_local_part(client):
    signup(client, "muhammadzeeshanhaideroffical", "password1", email="someoneelse@example.com")
    with tx() as conn:
        conn.execute(
            "UPDATE users SET is_admin = 1, plan_name = 'VIP' WHERE username = 'muhammadzeeshanhaideroffical'"
        )
    close_conn()
    page = client.get("/app")
    assert "0 of 100 used today" in page.text
    assert "Unlimited" not in page.text
    row = get_conn().execute(
        "SELECT email, is_admin, plan_name FROM users WHERE username = 'muhammadzeeshanhaideroffical'"
    ).fetchone()
    close_conn()
    assert row["email"] == "someoneelse@example.com"
    assert row["is_admin"] == 0
    assert row["plan_name"] == "Free"


def test_spaced_operator_email_and_queue_ignores_daily_speed(client, monkeypatch):
    signup(client, "boss", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    with tx() as conn:
        conn.execute(
            """UPDATE users SET email = '  MuhammadZeeshanHaiderOffical@gmail.com  ',
               is_admin = 0, plan_name = 'Free' WHERE username = 'boss'"""
        )
    close_conn()
    client.post("/logout")
    client.post("/login", data={"username": "boss", "password": "password1"}, follow_redirects=False)
    created = client.post(
        "/app/projects",
        data={
            "name": "All of them",
            "project_type": "indexing",
            "target_domain": "example.com",
            "daily_speed": "1",
            "urls": "\n".join(f"https://example.com/op{i}" for i in range(3)),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    async def fake_precheck(http, url, target, include, cache):
        return {
            "final_url": url, "http_status": 200, "is_live": True, "fetch_error": None,
            "content_type": "text/html", "meta_robots": "", "x_robots_tag": "", "noindex": False,
            "robots_disallow": False, "canonical": url, "canonicalized_away": False,
            "has_link": False, "link_rel": "none", "link_count": 0, "link_details_json": "[]",
        }

    monkeypatch.setattr("backlink_indexer.queue.precheck_url", fake_precheck)
    path = created.headers["location"].split("?")[0]
    client.post(path + "/process", follow_redirects=True)
    rows = get_conn().execute("SELECT queue_status FROM urls ORDER BY id").fetchall()
    stored = get_conn().execute("SELECT email, plan_name FROM users WHERE username = 'boss'").fetchone()
    close_conn()
    assert [row["queue_status"] for row in rows] == ["sent", "sent", "sent"]
    assert stored["email"] == "muhammadzeeshanhaideroffical@gmail.com"
    assert stored["plan_name"] == "VIP"


def test_engine_selector_and_batch_process(client, monkeypatch):
    signup(client, "picker", "password1")
    page = client.get("/app/new")
    assert "Target domain" not in page.text
    assert "Select all engines" in page.text
    assert "Not configured" in page.text
    assert 'class="engine-name"' in page.text
    assert 'class="engine-grid"' in page.text
    assert "bi-theme-2" in client.get("/login").text
    created = client.post(
        "/app/projects",
        data={
            "name": "Pick",
            "project_type": "indexing",
            "target_domain": "example.com",
            "daily_speed": "10",
            "engines_set": "1",
            "use_indexnow": "1",
            "urls": "\n".join(f"https://example.com/e{i}" for i in range(3)),
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    detail = client.get(path)
    assert "Target domain" not in detail.text
    assert "sent of" in detail.text
    assert "Select all engines" in detail.text
    assert "Off" in detail.text or "Not configured" in detail.text

    async def fake_precheck(http, url, target, include, cache, timeout=None):
        return {
            "final_url": url, "http_status": 200, "is_live": True, "fetch_error": None,
            "content_type": "text/html", "meta_robots": "", "x_robots_tag": "", "noindex": False,
            "robots_disallow": False, "canonical": url, "canonicalized_away": False,
            "has_link": False, "link_rel": "none", "link_count": 0, "link_details_json": "[]",
        }

    monkeypatch.setattr("backlink_indexer.queue.precheck_url", fake_precheck)
    monkeypatch.setattr("backlink_indexer.queue.batch_limit", lambda: 1)
    first = client.post(path + "/process", headers={"Accept": "application/json"})
    body = first.json()
    assert body["processed"] == 1
    assert body["queued"] == 2
    assert body["continue"] is True
    rows = get_conn().execute("SELECT google_state, bing_state, yandex_state, http_status FROM urls").fetchall()
    close_conn()
    sent = [row for row in rows if row["http_status"] == 200]
    assert sent
    assert all(row["google_state"] == "external_hub" for row in sent)


def test_crawl_hub_pages_sitemap_and_help(client, monkeypatch):
    monkeypatch.setattr("backlink_indexer.crawlhub.PAGE_SIZE", 2)
    signup(client, "hubber", "password1")
    created = client.post(
        "/app/projects",
        data={
            "name": "Crawl Hub",
            "project_type": "indexing",
            "daily_speed": "5",
            "urls": "\n".join(f"https://example.com/h{i}" for i in range(3)),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    path = created.headers["location"].split("?")[0]
    page = client.get("/hub/crawl-hub")
    assert page.status_code == 200
    assert "https://example.com/h0" in page.text
    assert "nofollow" not in page.text
    assert 'content="index,follow"' in page.text
    assert "canonical" in page.text
    assert "updated" in page.text
    second = client.get("/hub/crawl-hub/page/2")
    assert second.status_code == 200
    assert "https://example.com/h2" in second.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "/sitemaps/crawl-hub.xml" in sitemap.text
    assert "/sitemaps/crawl-hub-links.xml" in sitemap.text
    pages = client.get("/sitemaps/crawl-hub.xml")
    assert "/hub/crawl-hub" in pages.text
    links = client.get("/sitemaps/crawl-hub-links.xml")
    assert "https://example.com/h0" in links.text
    feed = client.get("/hub/crawl-hub/feed.xml")
    assert "<item>" in feed.text
    robots = client.get("/robots.txt")
    assert "Sitemap:" in robots.text
    assert "/sitemap.xml" in robots.text
    assert "Allow: /static/" in robots.text
    assert "Disallow: /static/" not in robots.text
    detail = client.get(path)
    assert "Crawl hub" in detail.text
    assert "Search Console" in detail.text
    assert "Published" in detail.text
    hidden = client.post(path + "/hub", data={}, follow_redirects=False)
    assert hidden.status_code == 303
    assert client.get("/hub/crawl-hub").status_code == 404


def test_csv_export(client):
    signup(client, "export", "password1")
    created = client.post(
        "/app/projects",
        data={"name": "Export", "project_type": "indexing", "target_domain": "example.com", "daily_speed": "4", "urls": "https://example.com/e"},
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    csv_resp = client.get(path + "/export.csv")
    assert csv_resp.status_code == 200
    assert "https://example.com/e" in csv_resp.text
    assert "unknown" in csv_resp.text


def test_backfill_publishes_projects_that_never_got_a_slug(client):
    signup(client, "slugger", "password1")
    created = client.post(
        "/app/projects",
        data={
            "name": "Zeeshan Haider",
            "project_type": "indexing",
            "daily_speed": "5",
            "urls": "https://example.com/old",
        },
        follow_redirects=False,
    )
    project_id = int(created.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    with tx() as conn:
        conn.execute("UPDATE projects SET slug='', publish_hub=0 WHERE id=?", (project_id,))
    close_conn()
    sitemap = client.get("/sitemap.xml")
    assert "/sitemaps/zeeshan-haider.xml" in sitemap.text
    assert "/sitemaps/zeeshan-haider-links.xml" in sitemap.text
    hub = client.get("/hub/zeeshan-haider")
    assert hub.status_code == 200
    assert "https://example.com/old" in hub.text
    kept = client.post(
        "/app/projects",
        data={"name": "Stay Off", "project_type": "indexing", "daily_speed": "5", "urls": "https://example.com/off"},
        follow_redirects=False,
    )
    kept_id = int(kept.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    with tx() as conn:
        conn.execute("UPDATE projects SET publish_hub=0 WHERE id=?", (kept_id,))
    close_conn()
    again = client.get("/sitemap.xml")
    assert "stay-off" not in again.text
    assert client.get("/hub/stay-off").status_code == 404


def test_hub_request_backfills_without_opening_the_sitemap(client):
    signup(client, "directhub", "password1")
    created = client.post(
        "/app/projects",
        data={
            "name": "Zeeshan Haider",
            "project_type": "indexing",
            "daily_speed": "5",
            "urls": "https://example.com/direct",
        },
        follow_redirects=False,
    )
    project_id = int(created.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
    with tx() as conn:
        conn.execute("UPDATE projects SET slug='', publish_hub=0 WHERE id=?", (project_id,))
    close_conn()
    hub = client.get("/hub/zeeshan-haider")
    assert hub.status_code == 200
    assert "https://example.com/direct" in hub.text
    again = client.get("/hub/zeeshan-haider")
    assert again.status_code == 200
    sitemap = client.get("/sitemap.xml")
    assert "/sitemaps/zeeshan-haider.xml" in sitemap.text
    pages = client.get("/sitemaps/zeeshan-haider.xml")
    assert pages.status_code == 200
    assert "/hub/zeeshan-haider" in pages.text


def test_link_type_column_filter_and_external_skip(client, monkeypatch):
    signup(client, "linker", "password1")
    with tx() as conn:
        conn.execute(
            "INSERT INTO owned_hosts (host, notes, created_at) VALUES ('owned.example', '', ?)",
            (utcnow(),),
        )
    close_conn()
    created = client.post(
        "/app/projects",
        data={
            "name": "Types",
            "project_type": "indexing",
            "daily_speed": "10",
            "urls": "https://owned.example/mine",
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    page = client.get(path)
    assert "Link type" in page.text
    assert "Where to send" in page.text
    assert "Add links" in page.text
    assert "summaryCount" in page.text
    assert "type-internal" in page.text
    added = client.post(
        path + "/urls",
        data={
            "link_type": "external",
            "urls": "https://other.example/out",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    external = client.get(path + "?filter=external")
    assert "other.example/out" in external.text
    assert "owned.example/mine" not in external.text
    internal = client.get(path + "?filter=internal")
    assert "owned.example/mine" in internal.text
    auto = client.post(
        path + "/urls",
        data={"link_type": "auto", "urls": "https://owned.example/auto\nhttps://stranger.example/x"},
        follow_redirects=False,
    )
    assert auto.status_code == 303
    stored = get_conn().execute("SELECT url, link_type FROM urls ORDER BY id").fetchall()
    kinds = {row["url"]: row["link_type"] for row in stored}
    assert kinds["https://owned.example/mine"] == "internal"
    assert kinds["https://other.example/out"] == "external"
    assert kinds["https://owned.example/auto"] == "internal"
    assert kinds["https://stranger.example/x"] == "external"
    close_conn()

    calls = {"bing": 0}

    async def fake_precheck(http, url, target, include, cache):
        return {
            "final_url": url, "http_status": 200, "is_live": True, "fetch_error": None,
            "content_type": "text/html", "meta_robots": "", "x_robots_tag": "", "noindex": False,
            "robots_disallow": False, "canonical": url, "canonicalized_away": False,
            "has_link": False, "link_rel": "none", "link_count": 0, "link_details_json": "[]",
        }

    async def fake_bing(*args, **kwargs):
        calls["bing"] += 1
        return {"ok": True, "state": "accepted", "detail": "should not run"}

    monkeypatch.setattr("backlink_indexer.queue.precheck_url", fake_precheck)
    monkeypatch.setattr("backlink_indexer.queue.bing_webmaster.submit_url", fake_bing)
    monkeypatch.setattr("backlink_indexer.queue.bing_webmaster.plan_submissions", fake_bing)
    client.post(path + "/process", follow_redirects=True)
    rows = get_conn().execute("SELECT url, google_state, bing_state FROM urls").fetchall()
    close_conn()
    by_url = {row["url"]: row for row in rows}
    assert by_url["https://other.example/out"]["google_state"] == "external_hub"
    assert by_url["https://other.example/out"]["bing_state"] == "external_hub"
    assert by_url["https://owned.example/mine"]["google_state"] == "not_configured"
    assert calls["bing"] == 0
