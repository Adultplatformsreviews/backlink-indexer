import asyncio

import httpx
import pytest

from backlink_indexer.database import close_conn, get_conn, set_setting
from backlink_indexer.services import bing_webmaster, indexnow, yandex_webmaster


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backlink_indexer.main import app

    with TestClient(app) as test_client:
        conn = get_conn()
        for table in (
            "submission_log", "urls", "batches", "notifications", "tickets",
            "api_keys", "sessions", "projects", "owned_hosts", "daily_usage", "login_codes", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        close_conn()
        yield test_client


def test_indexnow_hits_bing_and_yandex_endpoints():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "yandex.com" in str(request.url):
            return httpx.Response(422, text="host mismatch")
        return httpx.Response(200, text="ok")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await indexnow.submit_all(
                http, ["https://example.com/a"], "abcd1234ef", "https://example.com/abcd1234ef.txt"
            )

    results = asyncio.run(run())
    assert any("api.indexnow.org" in url for url in seen)
    assert any("www.bing.com/indexnow" in url for url in seen)
    assert any("yandex.com/indexnow" in url for url in seen)
    assert results["bing"]["state"] == "accepted"
    assert results["yandex"]["state"] == "rejected"
    assert results["bing"]["state"] != "indexed"


def test_bing_webmaster_accepts_without_marking_indexed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "SubmitUrlBatch" in str(request.url)
        assert request.url.params["apikey"] == "bing-key"
        assert b"https://example.com/a" in request.content
        return httpx.Response(200, json={"d": None})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await bing_webmaster.submit_url(
                http, "bing-key", "https://example.com/", "https://example.com/a"
            )

    result = asyncio.run(run())
    assert result["state"] == "accepted"
    assert result["ok"] is True
    assert "not an index" in result["detail"].lower()


def test_bing_webmaster_records_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="NotAuthorized")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await bing_webmaster.submit_url(
                http, "bing-key", "https://example.com/", "https://example.com/a"
            )

    result = asyncio.run(run())
    assert result["state"] == "rejected"
    assert "NotAuthorized" in result["detail"]
    assert result["state"] != "indexed"


def test_bing_daily_quota_blocks_submit(client):
    set_setting("bing_quota_date", bing_webmaster._today())
    set_setting("bing_quota_count", str(bing_webmaster.DAILY_LIMIT))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("quota should stop the request")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await bing_webmaster.submit_url(
                http, "bing-key", "https://example.com/", "https://example.com/a"
            )

    result = asyncio.run(run())
    assert result["state"] == "deferred"
    assert "quota" in result["detail"].lower()


def test_yandex_recrawl_accepted_is_not_indexed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "OAuth ya-token"
        assert request.url.path.endswith("/recrawl/queue")
        return httpx.Response(202, json={"task_id": "1"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await yandex_webmaster.recrawl_url(
                http, "ya-token", "user", "host", "https://example.com/a"
            )

    result = asyncio.run(run())
    assert result["state"] == "accepted"
    assert result["status"] == 202
    assert "not an index" in result["detail"].lower()


def test_yandex_recrawl_rejection_and_missing_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="HOST_NOT_VERIFIED")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            rejected = await yandex_webmaster.recrawl_url(
                http, "ya-token", "user", "host", "https://other.example/a"
            )
            skipped = await yandex_webmaster.recrawl_url(
                http, "", "user", "host", "https://example.com/a"
            )
            return rejected, skipped

    rejected, skipped = asyncio.run(run())
    assert rejected["state"] == "rejected"
    assert "HOST_NOT_VERIFIED" in rejected["detail"]
    assert skipped["state"] == "skipped"


def test_yandex_quota_remainder_is_read():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/recrawl/quota")
        return httpx.Response(200, json={"daily_quota": 10, "quota_remainder": 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await yandex_webmaster.fetch_quota(http, "ya-token", "user", "host")

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["remainder"] == 0
