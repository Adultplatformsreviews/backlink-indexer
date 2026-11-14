"""Public crawl hub for third-party URLs this app does not own."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from .database import get_conn, tx, utcnow
from .projects import ensure_slug
from .services import indexnow
from .services.secrets import setting_or_env
from .services.websub import ping_bing_sitemap

PAGE_SIZE = 100
PUBLIC_HOST = "https://backlink-indexer-zeta.vercel.app"


def crawl_base(request_base: str = "") -> str:
    from .database import get_setting
    from .config import PUBLIC_BASE_URL

    configured = (get_setting("public_base_url") or PUBLIC_BASE_URL or "").rstrip("/")
    if _public(configured):
        return configured
    request_base = (request_base or "").rstrip("/")
    if _public(request_base):
        return request_base
    return PUBLIC_HOST


def _public(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and host not in {"localhost", "127.0.0.1", "testserver"}


def hub_page_path(slug: str, page: int) -> str:
    if page <= 1:
        return f"/hub/{slug}"
    return f"/hub/{slug}/page/{page}"


def published_projects() -> list[dict]:
    rows = get_conn().execute(
        """SELECT * FROM projects
           WHERE publish_hub = 1 AND slug != ''
           ORDER BY id"""
    ).fetchall()
    return [dict(row) for row in rows]


def project_by_slug(slug: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return None
    project = dict(row)
    if not project.get("publish_hub"):
        return None
    return ensure_slug(project)


def _count(project_id: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM urls WHERE project_id = ? AND in_hub = 1",
        (project_id,),
    ).fetchone()
    return int(row["c"] or 0)


def hub_listing(project: dict, page: int) -> Optional[dict]:
    total = _count(project["id"])
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, int(page or 1))
    if page > pages:
        return None
    rows = get_conn().execute(
        """SELECT id, url, created_at, updated_at FROM urls
           WHERE project_id = ? AND in_hub = 1
           ORDER BY id ASC LIMIT ? OFFSET ?""",
        (project["id"], PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    updated = ""
    for row in rows:
        stamp = row["updated_at"] or row["created_at"] or ""
        if stamp > updated:
            updated = stamp
    if not updated:
        updated = project.get("updated_at") or utcnow()
    return {
        "urls": [dict(row) for row in rows],
        "page": page,
        "pages": pages,
        "total": total,
        "updated": updated,
    }


def newest_links(project_id: int, limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        """SELECT url, created_at, updated_at FROM urls
           WHERE project_id = ? AND in_hub = 1
           ORDER BY id DESC LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def annotate_rows(project: dict, rows: list[dict]) -> None:
    project = ensure_slug(project)
    published = bool(project.get("publish_hub"))
    ids = []
    if published:
        listed = get_conn().execute(
            "SELECT id FROM urls WHERE project_id = ? AND in_hub = 1 ORDER BY id ASC",
            (project["id"],),
        ).fetchall()
        ids = [int(row["id"]) for row in listed]
    rank = {url_id: index for index, url_id in enumerate(ids)}
    for row in rows:
        position = rank.get(int(row["id"]))
        if position is None or not published:
            row["hub_status"] = "Off"
            row["hub_href"] = ""
            continue
        page = position // PAGE_SIZE + 1
        row["hub_status"] = "Published"
        row["hub_href"] = hub_page_path(project["slug"], page)


def _record(url_ids: list[int], status: str, detail: str) -> None:
    if not url_ids:
        return
    now = utcnow()
    with tx() as conn:
        for url_id in url_ids:
            conn.execute(
                """INSERT INTO submission_log (url_id, method, status, detail, created_at)
                   VALUES (?, 'hub', ?, ?, ?)""",
                (url_id, status, detail[:500], now),
            )


async def ping_hub(client, project_id: int, url_ids: list[int], request_base: str = "") -> None:
    """Tell IndexNow and Bing about this project's hub pages. Never raises."""
    if not url_ids:
        return
    row = get_conn().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        return
    project = ensure_slug(dict(row))
    if not project.get("publish_hub"):
        _record(url_ids, "skipped", "Publish to crawl hub is off, so no hub page was pinged.")
        return
    base = crawl_base(request_base)
    page = hub_page_path(project["slug"], 1)
    hub_url = f"{base}{page}"
    sitemap_url = f"{base}/sitemaps/{project['slug']}.xml"
    links_url = f"{base}/sitemaps/{project['slug']}-links.xml"
    key, _source = setting_or_env("indexnow_key", "INDEXNOW_KEY")
    parts = [f"Hub page {hub_url}."]
    status = "accepted"
    if not _public(base):
        parts.append("Ping skipped because this host is not public.")
        status = "skipped"
    else:
        if key:
            try:
                results = await indexnow.submit_all(
                    client, [hub_url], key, f"{base}/{key}.txt"
                )
                bits = [f"{name}={item.get('detail') or item.get('state')}" for name, item in results.items()]
                parts.append("IndexNow " + "; ".join(bits) + ". Acceptance is not an index confirmation.")
                if any(item.get("state") == "rejected" for item in results.values()):
                    status = "rejected"
            except Exception as exc:
                parts.append(f"IndexNow ping failed: {exc}.")
                status = "rejected"
        else:
            parts.append("IndexNow key is not set, so the hub page was not submitted.")
            status = "skipped"
        try:
            bing = await ping_bing_sitemap(client, sitemap_url)
            parts.append(f"Bing sitemap ping: {bing.get('detail')}.")
        except Exception as exc:
            parts.append(f"Bing sitemap ping failed: {exc}.")
        parts.append(
            "Google no longer accepts sitemap pings. Submit "
            f"{sitemap_url} and {links_url} in Google Search Console."
        )
    _record(url_ids, status, " ".join(parts))
