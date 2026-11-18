from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

from ..config import USER_AGENT


PROVIDERS = {
    "serpapi": {
        "label": "SerpAPI",
        "docs": "https://serpapi.com/search-api",
    },
    "valueserp": {
        "label": "ValueSERP",
        "docs": "https://www.valueserp.com/docs",
    },
    "searchapi": {
        "label": "SearchApi.io",
        "docs": "https://www.searchapi.io/docs/google",
    },
    "custom": {
        "label": "Custom endpoint",
        "docs": "",
    },
}


def build_query(url: str) -> str:
    """Exact-URL site query used to check whether a page appears in Google."""
    return f"site:{url}"


def inurl_query(url: str) -> str:
    """Fallback when an exact site: query misses. Path on the same host only."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return f"inurl:{path} site:{host}"


async def serpapi_search(
    client: httpx.AsyncClient,
    api_key: str,
    engine: str,
    query: str,
    match_url: str,
) -> dict[str, Any]:
    """One SerpApi search. indexed is True/False, or None when the call failed.

    Yandex is reported as unverified when SerpApi rejects that engine.
    A hit is never invented. match_url is the page we are looking for.
    """
    if not api_key:
        return {"ok": False, "indexed": None, "detail": "No SerpApi key is configured.", "query": query, "engine": engine}
    try:
        data, _note = await _get(
            client,
            "https://serpapi.com/search.json",
            {"engine": engine, "q": query, "api_key": api_key, "num": 10},
        )
    except Exception as exc:
        return {
            "ok": False,
            "indexed": None,
            "unverified": engine == "yandex",
            "detail": f"{type(exc).__name__}: {exc}",
            "query": query,
            "engine": engine,
        }
    error = data.get("error")
    if error:
        return {
            "ok": False,
            "indexed": None,
            "unverified": engine == "yandex",
            "detail": str(error),
            "query": query,
            "engine": engine,
        }
    results = data.get("organic_results") or []
    links = [item.get("link") for item in results if isinstance(item, dict)]
    indexed = _match_results(match_url, links)
    return {
        "ok": True,
        "indexed": bool(indexed),
        "unverified": False,
        "detail": f"{'Indexed' if indexed else 'Not found in top results'} for {query}.",
        "query": query,
        "engine": engine,
        "hits": [link for link in links[:5] if link],
    }


async def check_indexed(
    client: httpx.AsyncClient,
    url: str,
    provider: str,
    api_key: str,
    endpoint: str = "",
) -> dict[str, Any]:
    if not api_key and provider != "custom":
        return {
            "ok": False,
            "indexed": None,
            "detail": "No SERP API key is configured in Settings. Status was not changed.",
        }
    provider = (provider or "serpapi").lower()
    query = build_query(url)
    try:
        if provider == "serpapi":
            data, raw_note = await _get(
                client,
                "https://serpapi.com/search.json",
                {"engine": "google", "q": query, "api_key": api_key, "num": 10},
            )
            results = data.get("organic_results") or []
            error = data.get("error")
            if error:
                return {"ok": False, "indexed": None, "detail": str(error), "query": query}
            indexed = _match_results(url, [r.get("link") for r in results])
            return {
                "ok": True,
                "indexed": indexed,
                "detail": f"{'Indexed' if indexed else 'Not found in top results'} for {query}.",
                "query": query,
                "hits": [r.get("link") for r in results[:5] if r.get("link")],
            }
        if provider == "valueserp":
            data, _ = await _get(
                client,
                "https://api.valueserp.com/search",
                {"api_key": api_key, "q": query, "engine": "google", "num": 10},
            )
            if data.get("request_info") and data["request_info"].get("success") is False:
                return {
                    "ok": False,
                    "indexed": None,
                    "detail": str(data.get("request_info")),
                    "query": query,
                }
            results = data.get("organic_results") or []
            indexed = _match_results(url, [r.get("link") for r in results])
            return {
                "ok": True,
                "indexed": indexed,
                "detail": f"{'Indexed' if indexed else 'Not found in top results'} for {query}.",
                "query": query,
                "hits": [r.get("link") for r in results[:5] if r.get("link")],
            }
        if provider == "searchapi":
            data, _ = await _get(
                client,
                "https://www.searchapi.io/api/v1/search",
                {"engine": "google", "q": query, "api_key": api_key, "num": 10},
            )
            if data.get("error"):
                return {"ok": False, "indexed": None, "detail": str(data.get("error")), "query": query}
            results = data.get("organic_results") or []
            indexed = _match_results(url, [r.get("link") for r in results])
            return {
                "ok": True,
                "indexed": indexed,
                "detail": f"{'Indexed' if indexed else 'Not found in top results'} for {query}.",
                "query": query,
                "hits": [r.get("link") for r in results[:5] if r.get("link")],
            }
        if provider == "custom":
            if not endpoint:
                return {
                    "ok": False,
                    "indexed": None,
                    "detail": "Custom SERP endpoint is empty.",
                    "query": query,
                }
            filled = (
                endpoint.replace("{query}", quote(query))
                .replace("{url}", quote(url))
                .replace("{api_key}", quote(api_key))
            )
            resp = await client.get(
                filled,
                timeout=30,
                headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {api_key}"},
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "indexed": None,
                    "detail": f"Custom SERP HTTP {resp.status_code}: {resp.text[:400]}",
                    "query": query,
                }
            data = resp.json()
            # Accept a few common shapes; never invent a result.
            results = (
                data.get("organic_results")
                or data.get("results")
                or data.get("items")
                or []
            )
            links = []
            for r in results:
                if isinstance(r, str):
                    links.append(r)
                elif isinstance(r, dict):
                    links.append(r.get("link") or r.get("url") or r.get("linkUrl"))
            if "indexed" in data and isinstance(data["indexed"], bool):
                indexed = data["indexed"]
            else:
                indexed = _match_results(url, links)
            return {
                "ok": True,
                "indexed": indexed,
                "detail": f"{'Indexed' if indexed else 'Not found in top results'} for {query}.",
                "query": query,
                "hits": [l for l in links[:5] if l],
            }
        return {"ok": False, "indexed": None, "detail": f"Unknown SERP provider: {provider}"}
    except Exception as exc:
        return {
            "ok": False,
            "indexed": None,
            "detail": f"SERP check failed: {type(exc).__name__}: {exc}",
            "query": query,
        }


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> tuple[dict, str]:
    resp = await client.get(
        url, params=params, timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json(), ""


def _match_results(target: str, links: list[Optional[str]]) -> bool:
    def norm(u: str) -> str:
        p = urlparse(u.strip())
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        host = (p.hostname or "").lower().removeprefix("www.")
        return f"{host}{path}"

    target_n = norm(target)
    for link in links:
        if not link:
            continue
        if norm(link) == target_n:
            return True
    return False
