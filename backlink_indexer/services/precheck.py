from __future__ import annotations

import json
import time
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from ..config import FETCH_TIMEOUT, MAX_BODY_BYTES, USER_AGENT
from .urlutil import (
    host_matches,
    is_safe_public_url,
    resolve_url,
    robots_url,
    same_document,
)

GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


class RobotsCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Optional[RobotFileParser], Optional[str]]] = {}
        self._ttl = 600.0

    async def allowed(
        self, client: httpx.AsyncClient, url: str, timeout: Optional[float] = None
    ) -> tuple[Optional[bool], Optional[str]]:
        key = robots_url(url)
        now = time.time()
        entry = self._cache.get(key)
        if not entry or now - entry[0] > self._ttl:
            parser, error = await self._fetch(client, key, timeout)
            self._cache[key] = (now, parser, error)
        else:
            _, parser, error = entry
        if error and parser is None:
            return None, error
        if parser is None:
            return True, None
        # Check both Googlebot and the generic * rule. A disallow for Googlebot
        # is what matters for "will Google crawl this page".
        google_ok = parser.can_fetch(GOOGLEBOT_UA, url)
        star_ok = parser.can_fetch("*", url)
        # If Googlebot is mentioned, its rule wins; otherwise * applies.
        allowed = google_ok
        if not allowed:
            return False, None
        if not star_ok and google_ok:
            # Explicit Googlebot allow (or no Googlebot rule plus * disallow
            # already reflected by can_fetch). Treat as allowed if Googlebot can.
            return True, None
        return True, None

    async def _fetch(
        self, client: httpx.AsyncClient, robots: str, timeout: Optional[float] = None
    ) -> tuple[Optional[RobotFileParser], Optional[str]]:
        try:
            resp = await client.get(
                robots,
                timeout=FETCH_TIMEOUT if timeout is None else timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "text/plain,*/*"},
            )
        except Exception as exc:
            return None, f"robots.txt fetch failed: {exc}"
        if resp.status_code == 404:
            parser = RobotFileParser()
            parser.parse([])
            return parser, None
        if resp.status_code >= 400:
            # Conservative: if robots.txt is unreadable (403/5xx), do not claim disallow.
            return None, f"robots.txt HTTP {resp.status_code}"
        text = resp.text
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        return parser, None


async def precheck_url(
    client: httpx.AsyncClient,
    url: str,
    target_domain: str,
    include_subdomains: bool = True,
    robots_cache: Optional[RobotsCache] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "http_status": None,
        "is_live": None,
        "fetch_error": None,
        "final_url": None,
        "content_type": None,
        "meta_robots": None,
        "x_robots_tag": None,
        "noindex": False,
        "robots_disallow": None,
        "canonical": None,
        "canonicalized_away": False,
        "has_link": False,
        "link_rel": "none",
        "link_count": 0,
        "link_details": [],
    }

    ok, reason = is_safe_public_url(url)
    if not ok:
        result["fetch_error"] = reason
        result["is_live"] = False
        return result

    if timeout is None:
        timeout = getattr(client, "bi_timeout", None)
    limit = FETCH_TIMEOUT if timeout is None else timeout
    robots_cache = robots_cache or RobotsCache()
    try:
        allowed, robots_error = await robots_cache.allowed(client, url, timeout=limit)
        result["robots_disallow"] = (False if allowed is None else (not allowed))
        if robots_error and allowed is None:
            # Unknown — leave robots_disallow as None (stored as NULL).
            result["robots_disallow"] = None
            result["fetch_error"] = None  # page fetch may still succeed
    except Exception as exc:
        result["robots_disallow"] = None
        robots_error = str(exc)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = await client.get(
            url,
            timeout=limit,
            follow_redirects=True,
            headers=headers,
        )
    except httpx.TimeoutException:
        result["fetch_error"] = "Request timed out"
        result["is_live"] = False
        return result
    except Exception as exc:
        result["fetch_error"] = f"Fetch failed: {type(exc).__name__}: {exc}"
        result["is_live"] = False
        return result

    result["http_status"] = resp.status_code
    result["final_url"] = str(resp.url)
    result["content_type"] = resp.headers.get("content-type", "")
    result["is_live"] = 200 <= resp.status_code < 400

    xrobots = _header_values(resp, "x-robots-tag")
    result["x_robots_tag"] = ", ".join(xrobots) if xrobots else None
    noindex_header = _has_noindex(result["x_robots_tag"] or "")

    body = resp.content[:MAX_BODY_BYTES]
    ctype = (result["content_type"] or "").lower()
    is_html = "html" in ctype or "xml" in ctype or not ctype
    # Some servers omit content-type; sniff a little.
    if not is_html:
        sniff = body[:256].lstrip().lower()
        if sniff.startswith(b"<!doctype html") or sniff.startswith(b"<html") or sniff.startswith(b"<?xml"):
            is_html = True

    html = ""
    if is_html:
        encoding = resp.encoding or "utf-8"
        try:
            html = body.decode(encoding, errors="replace")
        except Exception:
            html = body.decode("utf-8", errors="replace")

    if html:
        soup = BeautifulSoup(html, "lxml")
        meta_robots = _meta_robots(soup)
        result["meta_robots"] = meta_robots or None
        result["noindex"] = bool(noindex_header or _has_noindex(meta_robots))

        canonical = _canonical(soup, str(resp.url))
        result["canonical"] = canonical
        if canonical:
            result["canonicalized_away"] = not same_document(str(resp.url), canonical)
        else:
            result["canonicalized_away"] = False

        links = _target_links(soup, str(resp.url), target_domain, include_subdomains)
        result["link_details"] = links
        result["link_count"] = len(links)
        result["has_link"] = len(links) > 0
        result["link_rel"] = _summarize_rel(links)
    else:
        result["noindex"] = bool(noindex_header)
        if not result["is_live"]:
            if not result["fetch_error"]:
                result["fetch_error"] = f"HTTP {resp.status_code}"
        elif "html" not in ctype:
            result["fetch_error"] = f"Non-HTML response ({ctype or 'unknown type'})"

    result["link_details_json"] = json.dumps(result["link_details"], ensure_ascii=False)
    return result


def _header_values(resp: httpx.Response, name: str) -> list[str]:
    values = []
    for k, v in resp.headers.items():
        if k.lower() == name.lower():
            values.append(v)
    return values


def _meta_robots(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or tag.get("http-equiv") or "").strip().lower()
        if name in {"robots", "googlebot", "googlebot-news"}:
            content = (tag.get("content") or "").strip()
            if content:
                parts.append(f"{name}: {content}")
    return "; ".join(parts)


def _has_noindex(value: str) -> bool:
    if not value:
        return False
    tokens = [t.strip().lower() for t in value.replace(";", ",").split(",")]
    # Handle "robots: noindex, follow" style combined strings.
    flat = []
    for t in tokens:
        if ":" in t and not t.startswith("http"):
            # "robots: noindex" or "googlebot: noindex, nofollow"
            rhs = t.split(":", 1)[1]
            flat.extend([x.strip() for x in rhs.replace(";", ",").split(",")])
        flat.append(t)
    return "none" in flat or "noindex" in flat


def _canonical(soup: BeautifulSoup, base: str) -> Optional[str]:
    # Prefer <link rel="canonical"> in head; fall back to any in the document.
    candidates = []
    for tag in soup.find_all("link"):
        rel = " ".join(tag.get("rel") or []).lower()
        if "canonical" in rel.split():
            href = tag.get("href") or ""
            resolved = resolve_url(base, href)
            if resolved:
                candidates.append(resolved)
    return candidates[0] if candidates else None


def _target_links(
    soup: BeautifulSoup,
    base: str,
    target_domain: str,
    include_subdomains: bool,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    tags = list(soup.find_all("a")) + list(soup.find_all("area"))
    for tag in tags:
        href = tag.get("href") or ""
        resolved = resolve_url(base, href)
        if not resolved:
            continue
        host = urlparse(resolved).hostname or ""
        if not host_matches(host, target_domain, include_subdomains):
            continue
        rel_tokens = [r.lower() for r in (tag.get("rel") or [])]
        # rel may also be a string
        if isinstance(tag.get("rel"), str):
            rel_tokens = tag.get("rel").split()
            rel_tokens = [r.lower() for r in rel_tokens]
        nofollow = "nofollow" in rel_tokens
        sponsored = "sponsored" in rel_tokens
        ugc = "ugc" in rel_tokens
        kind = "dofollow"
        if nofollow:
            kind = "nofollow"
        elif sponsored:
            kind = "sponsored"
        elif ugc:
            kind = "ugc"
        text = " ".join(tag.get_text(" ", strip=True).split())[:200]
        key = (resolved, kind)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "href": resolved,
                "rel": kind,
                "rel_tokens": rel_tokens,
                "text": text,
            }
        )
        if len(found) >= 50:
            break
    return found


def _summarize_rel(links: list[dict[str, Any]]) -> str:
    if not links:
        return "none"
    kinds = {l.get("rel") or "dofollow" for l in links}
    if kinds == {"dofollow"}:
        return "dofollow"
    if kinds == {"nofollow"}:
        return "nofollow"
    if kinds == {"sponsored"}:
        return "sponsored"
    if kinds == {"ugc"}:
        return "ugc"
    if "dofollow" in kinds and (kinds - {"dofollow"}):
        return "mixed"
    return "mixed"
