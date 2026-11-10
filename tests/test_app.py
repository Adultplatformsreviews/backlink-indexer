import re

import pytest
from fastapi.testclient import TestClient

from backlink_indexer.database import close_conn, get_conn, resolve_database_url
from backlink_indexer.main import app


@pytest.fixture
def client():
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
            "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        close_conn()
        yield test_client


def signup(client, username="ada", password="password1"):
    response = client.post("/signup", data={"username": username, "password": password}, follow_redirects=False)
    assert response.status_code == 303
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
    assert "95" in credits.text

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
    assert all(row["google_state"] == "unknown" for row in rows)
    second = client.post(project_path + "/process", follow_redirects=True)
    assert "Nothing was due" in second.text


def test_indexnow_submission_is_not_indexed(client, monkeypatch):
    signup(client, "owner", "password1")
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

    async def fake_submit(http, urls, key, key_location):
        return {"ok": True, "detail": "HTTP 200", "count": 1}

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
    assert row["yandex_state"] == "submitted"
    assert row["bing_state"] == "submitted"
    assert row["google_state"] == "unknown"


def test_manual_mark_and_pagination(client):
    signup(client, "mia", "password1")
    created = client.post(
        "/app/projects",
        data={
            "name": "Pages",
            "project_type": "check",
            "target_domain": "example.com",
            "daily_speed": "10",
            "urls": "\n".join(f"https://example.com/c{i}" for i in range(6)),
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?")[0]
    page2 = client.get(path + "?per=5&page=2")
    assert "c0" in page2.text
    assert "2 / 2" in page2.text
    listed = client.get(path + "?per=25")
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
