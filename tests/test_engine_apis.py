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


def test_bing_match_www_and_longest_path_prefix():
    sites = [
        "https://www.example.com/",
        "https://user.github.io/repo/",
        "https://user.github.io/repo/docs/",
        "https://other.github.io/repo/",
    ]
    assert bing_webmaster.match_site("https://example.com/page", sites) == "https://www.example.com/"
    assert bing_webmaster.match_site("https://user.github.io/repo/page", sites) == "https://user.github.io/repo/"
    assert bing_webmaster.match_site("https://user.github.io/repo/docs/a", sites) == "https://user.github.io/repo/docs/"
    assert bing_webmaster.match_site("https://user.github.io/other", sites) is None


def test_bing_plan_groups_by_site_and_skips_unverified():
    set_setting("bing_sites_json", "")
    set_setting("bing_sites_at", "")
    set_setting("bing_quota_json", "")
    set_setting("bing_quota_date", "2000-01-01")
    set_setting("bing_quota_count", "0")
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetUrlSubmissionQuota"):
            site = request.url.params.get("siteUrl", "")
            quota = 1 if "feetfinder.com" in site else 10
            return httpx.Response(200, json={"d": {"DailyQuota": quota}})
        import json
        posts.append(json.loads(request.content.decode()))
        return httpx.Response(200, text="")

    sites = ["https://feetfinder.com/", "https://user.github.io/repo/"]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await bing_webmaster.plan_submissions(
                http,
                "bing-key",
                [
                    "https://feetfinder.com/a",
                    "https://www.feetfinder.com/b",
                    "https://user.github.io/repo/page",
                    "https://not-verified.example/z",
                ],
                sites,
            )

    result = asyncio.run(run())
    assert result["https://not-verified.example/z"]["state"] == "not_verified"
    assert result["https://feetfinder.com/a"]["state"] == "accepted"
    assert "feetfinder.com" in result["https://feetfinder.com/a"]["detail"]
    assert result["https://www.feetfinder.com/b"]["state"] == "deferred"
    assert result["https://user.github.io/repo/page"]["state"] == "accepted"
    assert len(posts) == 2
    assert {item["siteUrl"] for item in posts} == {"https://feetfinder.com/", "https://user.github.io/repo/"}


def test_get_user_sites_keeps_verified_and_caches():
    set_setting("bing_sites_json", "")
    set_setting("bing_sites_at", "")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "d": [
                    {"SiteUrl": "https://feetfinder.com/", "IsVerified": True},
                    {"SiteUrl": "https://secret.example/", "IsVerified": False},
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            first = await bing_webmaster.get_user_sites(http, "bing-key")
            second = await bing_webmaster.get_user_sites(http, "bing-key")
            return first, second

    first, second = asyncio.run(run())
    assert calls["n"] == 1
    assert first == ["https://feetfinder.com/"]
    assert second == first


def test_not_verified_does_not_replace_an_indexnow_acceptance():
    from backlink_indexer.queue import _prefer

    assert _prefer("accepted", "not_verified") == "accepted"
    assert _prefer("off", "not_verified") == "not_verified"
