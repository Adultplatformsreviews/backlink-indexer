from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import (
    APP_PASSWORD,
    APP_USERNAME,
    DATA_DIR,
    DEFAULT_TARGET_DOMAIN,
    FETCH_CONCURRENCY,
    PUBLIC_BASE_URL,
    SA_PATH,
    USER_AGENT,
)
from .database import (
    get_conn,
    get_setting,
    get_settings,
    init_db,
    row_to_dict,
    rows_to_dicts,
    set_setting,
    set_settings,
    tx,
    utcnow,
)
from .services import google_indexing, indexnow, serp, websub
from .services.precheck import RobotsCache, precheck_url
from .services.urlutil import host_matches, host_of, normalize_submitted, registrable_host

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(
    title="Backlink Indexer",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

_jobs: dict[str, asyncio.Task] = {}
_http_client: Optional[httpx.AsyncClient] = None


def public_base(request: Optional[Request] = None) -> str:
    configured = get_setting("public_base_url") or PUBLIC_BASE_URL
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def hub_urls(request: Optional[Request] = None) -> dict[str, str]:
    base = public_base(request)
    return {
        "base": base,
        "hub": f"{base}/hub",
        "sitemap": f"{base}/hub/sitemap.xml",
        "rss": f"{base}/hub/feed.xml",
        "atom": f"{base}/hub/atom.xml",
        "robots": f"{base}/robots.txt",
    }


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not APP_USERNAME or not APP_PASSWORD:
            return await call_next(request)
        path = request.url.path
        # Public crawl surfaces must stay unauthenticated.
        public_prefixes = (
            "/hub",
            "/robots.txt",
            "/favicon.ico",
            "/static/",
        )
        if path in {"/robots.txt", "/favicon.ico"} or path.startswith(public_prefixes):
            return await call_next(request)
        # IndexNow key file: /{key}.txt
        if path.endswith(".txt") and path.count("/") == 1:
            return await call_next(request)
        auth = request.headers.get("authorization") or ""
        expected = "Basic " + __import__("base64").b64encode(
            f"{APP_USERNAME}:{APP_PASSWORD}".encode()
        ).decode()
        if auth != expected:
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Backlink Indexer"'},
                media_type="text/plain",
            )
        return await call_next(request)


app.add_middleware(BasicAuthMiddleware)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    global _http_client
    _http_client = httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
        limits=httpx.Limits(max_connections=FETCH_CONCURRENCY + 4, max_keepalive_connections=8),
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    if _http_client is not None:
        await _http_client.aclose()


def client() -> httpx.AsyncClient:
    assert _http_client is not None
    return _http_client


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "version": __version__,
            "default_target": get_setting("default_target_domain", DEFAULT_TARGET_DOMAIN),
            "hub": hub_urls(request),
        },
    )


@app.get("/favicon.ico")
async def favicon():
    path = ROOT / "static" / "favicon.svg"
    if path.exists():
        return FileResponse(path, media_type="image/svg+xml")
    return PlainTextResponse("", status_code=204)


@app.get("/robots.txt")
async def robots_txt(request: Request):
    body = (
        "User-agent: *\n"
        "Allow: /hub\n"
        "Allow: /hub/\n"
        "Allow: /robots.txt\n"
        "Disallow: /api/\n"
        "Disallow: /static/\n"
        f"Sitemap: {hub_urls(request)['sitemap']}\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@app.get("/hub", response_class=HTMLResponse)
async def hub_html(request: Request):
    rows = _hub_url_rows()
    return templates.TemplateResponse(
        request,
        "hub.html",
        {
            "urls": rows,
            "hub": hub_urls(request),
            "generated": utcnow(),
            "count": len(rows),
        },
    )


@app.get("/hub/sitemap.xml")
async def hub_sitemap(request: Request):
    rows = _hub_url_rows()
    lastmod_default = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    # Include the hub itself plus every submitted discovery URL.
    hub = hub_urls(request)
    parts.append("  <url>")
    parts.append(f"    <loc>{xml_escape(hub['hub'])}</loc>")
    parts.append(f"    <lastmod>{lastmod_default}</lastmod>")
    parts.append("  </url>")
    for row in rows:
        lastmod = (row.get("updated_at") or row.get("created_at") or lastmod_default)[:10]
        parts.append("  <url>")
        parts.append(f"    <loc>{xml_escape(row['url'])}</loc>")
        parts.append(f"    <lastmod>{xml_escape(lastmod)}</lastmod>")
        parts.append("  </url>")
    parts.append("</urlset>")
    xml = "\n".join(parts) + "\n"
    return Response(xml, media_type="application/xml")


@app.get("/hub/feed.xml")
async def hub_rss(request: Request):
    rows = _hub_url_rows()
    hub = hub_urls(request)
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for row in rows[:200]:
        title = xml_escape(row["url"])
        pub = _rss_date(row.get("created_at"))
        desc = "Submitted backlink URL for crawl discovery."
        if row.get("has_link") == 1:
            desc += " Pre-check found a link to the target domain."
        elif row.get("has_link") == 0:
            desc += " Pre-check did not find a link to the target domain."
        items.append(
            "    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <link>{xml_escape(row['url'])}</link>\n"
            f"      <guid isPermaLink=\"true\">{xml_escape(row['url'])}</guid>\n"
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <description>{xml_escape(desc)}</description>\n"
            "    </item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Backlink Indexer discovery feed</title>\n"
        f"    <link>{xml_escape(hub['hub'])}</link>\n"
        "    <description>Submitted backlink URLs listed for crawl discovery.</description>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f'    <atom:link href="{xml_escape(hub["rss"])}" rel="self" type="application/rss+xml"/>\n'
        f'    <atom:link href="{xml_escape(get_setting("websub_hub") or websub.DEFAULT_HUB)}" rel="hub"/>\n'
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
    return Response(xml, media_type="application/rss+xml")


@app.get("/hub/atom.xml")
async def hub_atom(request: Request):
    rows = _hub_url_rows()
    hub = hub_urls(request)
    now = utcnow()
    items = []
    for row in rows[:200]:
        updated = row.get("updated_at") or row.get("created_at") or now
        items.append(
            "  <entry>\n"
            f"    <title>{xml_escape(row['url'])}</title>\n"
            f'    <link href="{xml_escape(row["url"])}"/>\n'
            f"    <id>{xml_escape(row['url'])}</id>\n"
            f"    <updated>{xml_escape(updated)}</updated>\n"
            "    <summary>Submitted backlink URL for crawl discovery.</summary>\n"
            "  </entry>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>Backlink Indexer discovery feed</title>\n"
        f'  <link href="{xml_escape(hub["atom"])}" rel="self"/>\n'
        f'  <link href="{xml_escape(hub["hub"])}"/>\n'
        f'  <link href="{xml_escape(get_setting("websub_hub") or websub.DEFAULT_HUB)}" rel="hub"/>\n'
        f"  <id>{xml_escape(hub['atom'])}</id>\n"
        f"  <updated>{now}</updated>\n"
        + "\n".join(items)
        + "\n</feed>\n"
    )
    return Response(xml, media_type="application/atom+xml")


@app.get("/{key}.txt")
async def indexnow_key_file(key: str):
    configured = get_setting("indexnow_key")
    if not configured or key != configured:
        raise HTTPException(status_code=404, detail="Unknown IndexNow key")
    return PlainTextResponse(configured, media_type="text/plain")


# ---------------------------------------------------------------------------
# API: projects, batches, urls
# ---------------------------------------------------------------------------

@app.get("/api/meta")
async def api_meta(request: Request):
    settings = get_settings()
    return {
        "version": __version__,
        "hub": hub_urls(request),
        "has_indexnow_key": bool(settings.get("indexnow_key")),
        "has_serp_key": bool(settings.get("serp_api_key")),
        "has_google_sa": google_indexing.service_account_present(),
        "public_base_url": public_base(request),
        "serp_provider": settings.get("serp_provider") or "serpapi",
        "hub_include": settings.get("hub_include") or "all",
    }


@app.get("/api/projects")
async def list_projects():
    rows = get_conn().execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id) AS url_count,
               (SELECT COUNT(*) FROM batches b WHERE b.project_id = p.id) AS batch_count
        FROM projects p
        ORDER BY p.id DESC
        """
    ).fetchall()
    return {"projects": rows_to_dicts(rows)}


@app.post("/api/projects")
async def create_project(payload: dict[str, Any]):
    name = (payload.get("name") or "").strip() or "Untitled project"
    target = (payload.get("target_domain") or get_setting("default_target_domain") or DEFAULT_TARGET_DOMAIN).strip()
    target = registrable_host(target) or target
    include = 1 if payload.get("include_subdomains", True) else 0
    now = utcnow()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO projects (name, target_domain, include_subdomains, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, target, include, now, now),
        )
        pid = cur.lastrowid
    return {"project": _get_project(pid)}


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: int, payload: dict[str, Any]):
    project = _get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    name = payload.get("name", project["name"])
    target = payload.get("target_domain", project["target_domain"])
    target = registrable_host(str(target).strip()) or project["target_domain"]
    include = project["include_subdomains"]
    if "include_subdomains" in payload:
        include = 1 if payload.get("include_subdomains") else 0
    with tx() as conn:
        conn.execute(
            "UPDATE projects SET name=?, target_domain=?, include_subdomains=?, updated_at=? WHERE id=?",
            (str(name).strip() or project["name"], target, include, utcnow(), project_id),
        )
    return {"project": _get_project(project_id)}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int):
    project = _get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    remaining = get_conn().execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    if remaining <= 1:
        raise HTTPException(400, "Keep at least one project.")
    with tx() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"ok": True}


@app.get("/api/projects/{project_id}/batches")
async def list_batches(project_id: int):
    if not _get_project(project_id):
        raise HTTPException(404, "Project not found")
    rows = get_conn().execute(
        """
        SELECT b.*, (SELECT COUNT(*) FROM urls u WHERE u.batch_id = b.id) AS url_count
        FROM batches b WHERE b.project_id = ? ORDER BY b.id DESC
        """,
        (project_id,),
    ).fetchall()
    return {"batches": rows_to_dicts(rows)}


@app.get("/api/projects/{project_id}/stats")
async def project_stats(project_id: int):
    if not _get_project(project_id):
        raise HTTPException(404, "Project not found")
    row = get_conn().execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_live = 1 THEN 1 ELSE 0 END) AS live,
            SUM(CASE WHEN is_live = 0 THEN 1 ELSE 0 END) AS dead,
            SUM(CASE WHEN has_link = 1 THEN 1 ELSE 0 END) AS has_link,
            SUM(CASE WHEN has_link = 0 AND last_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS no_link,
            SUM(CASE WHEN noindex = 1 OR robots_disallow = 1 OR canonicalized_away = 1 THEN 1 ELSE 0 END) AS blocked,
            SUM(CASE WHEN index_status IN ('indexed', 'manual_indexed') THEN 1 ELSE 0 END) AS indexed,
            SUM(CASE WHEN index_status IN ('not_indexed', 'manual_not_indexed') THEN 1 ELSE 0 END) AS not_indexed,
            SUM(CASE WHEN last_checked_at IS NULL THEN 1 ELSE 0 END) AS unchecked,
            SUM(CASE WHEN submission_status = 'pending' THEN 1 ELSE 0 END) AS pending_submit
        FROM urls WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()
    data = row_to_dict(row) or {}
    for k, v in list(data.items()):
        data[k] = int(v or 0)
    return {"stats": data}


@app.get("/api/projects/{project_id}/urls")
async def list_urls(
    project_id: int,
    q: str = "",
    batch_id: Optional[int] = None,
    filter: str = "all",
    offset: int = 0,
    limit: int = 200,
):
    if not _get_project(project_id):
        raise HTTPException(404, "Project not found")
    where = ["u.project_id = ?"]
    params: list[Any] = [project_id]
    if batch_id:
        where.append("u.batch_id = ?")
        params.append(batch_id)
    if q.strip():
        where.append("u.url LIKE ?")
        params.append(f"%{q.strip()}%")
    clause = _filter_clause(filter, alias="u.")
    if clause:
        where.append(clause)
    where_sql = " AND ".join(where)
    total = get_conn().execute(
        f"SELECT COUNT(*) AS c FROM urls u WHERE {where_sql}", params
    ).fetchone()["c"]
    rows = get_conn().execute(
        f"""SELECT u.*, b.name AS batch_name
            FROM urls u
            LEFT JOIN batches b ON b.id = u.batch_id
            WHERE {where_sql}
            ORDER BY u.id DESC
            LIMIT ? OFFSET ?""",
        [*params, max(1, min(limit, 1000)), max(0, offset)],
    ).fetchall()
    items = []
    for r in rows:
        item = row_to_dict(r)
        if item and item.get("link_details"):
            try:
                item["link_details"] = json.loads(item["link_details"])
            except Exception:
                pass
        items.append(item)
    return {"urls": items, "total": total, "offset": offset, "limit": limit}


@app.post("/api/projects/{project_id}/urls")
async def add_urls(project_id: int, payload: dict[str, Any]):
    project = _get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    raw_list = payload.get("urls") or []
    if isinstance(raw_list, str):
        raw_lines = raw_list.splitlines()
    else:
        raw_lines = [str(x) for x in raw_list]
    batch_name = (payload.get("batch_name") or "").strip() or f"Batch {utcnow()[:16].replace('T', ' ')} UTC"
    return _ingest_urls(project_id, raw_lines, batch_name)


@app.post("/api/projects/{project_id}/urls/upload")
async def upload_urls(
    project_id: int,
    file: UploadFile = File(...),
    batch_name: str = Form(""),
):
    project = _get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    lines = _parse_upload(text, file.filename or "upload.txt")
    name = batch_name.strip() or (file.filename or f"Upload {utcnow()[:16]}")
    return _ingest_urls(project_id, lines, name)


@app.delete("/api/urls")
async def delete_urls(payload: dict[str, Any]):
    ids = [int(i) for i in (payload.get("ids") or [])]
    if not ids:
        return {"deleted": 0}
    with tx() as conn:
        conn.execute(
            f"DELETE FROM urls WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
    return {"deleted": len(ids)}


@app.post("/api/urls/toggle-hub")
async def toggle_hub(payload: dict[str, Any]):
    ids = [int(i) for i in (payload.get("ids") or [])]
    in_hub = 1 if payload.get("in_hub", True) else 0
    if not ids:
        return {"updated": 0}
    with tx() as conn:
        conn.execute(
            f"UPDATE urls SET in_hub=?, updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
            [in_hub, utcnow(), *ids],
        )
    return {"updated": len(ids), "in_hub": bool(in_hub)}


@app.post("/api/urls/mark-indexed")
async def mark_indexed(payload: dict[str, Any]):
    url_id = int(payload.get("id"))
    indexed = bool(payload.get("indexed"))
    status = "manual_indexed" if indexed else "manual_not_indexed"
    now = utcnow()
    with tx() as conn:
        cur = conn.execute(
            """UPDATE urls SET index_status=?, index_source='manual', index_note=?,
               last_index_check_at=?, updated_at=? WHERE id=?""",
            (status, "Manually marked by user.", now, now, url_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "URL not found")
    return {"ok": True, "index_status": status}


@app.get("/api/projects/{project_id}/export.csv")
async def export_csv(project_id: int, filter: str = "all", q: str = "", batch_id: Optional[int] = None):
    project = _get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    where = ["u.project_id = ?"]
    params: list[Any] = [project_id]
    if batch_id:
        where.append("u.batch_id = ?")
        params.append(batch_id)
    if q.strip():
        where.append("u.url LIKE ?")
        params.append(f"%{q.strip()}%")
    clause = _filter_clause(filter, alias="u.")
    if clause:
        where.append(clause)
    rows = get_conn().execute(
        f"""SELECT u.*, b.name AS batch_name
            FROM urls u LEFT JOIN batches b ON b.id = u.batch_id
            WHERE {' AND '.join(where)}
            ORDER BY u.id ASC""",
        params,
    ).fetchall()
    buf = io.StringIO()
    fields = [
        "id", "url", "final_url", "batch_name", "http_status", "is_live",
        "fetch_error", "meta_robots", "x_robots_tag", "noindex",
        "robots_disallow", "canonical", "canonicalized_away",
        "has_link", "link_rel", "link_count", "in_hub",
        "submission_status", "indexnow_status", "google_index_status",
        "index_status", "index_source", "index_note",
        "last_checked_at", "last_submitted_at", "last_index_check_at",
        "created_at",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        d = row_to_dict(r) or {}
        writer.writerow({k: d.get(k, "") for k in fields})
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"backlink-indexer-{project['name'].replace(' ', '_')[:40]}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Jobs: precheck, indexnow, google, serp, hub ping
# ---------------------------------------------------------------------------

@app.post("/api/urls/precheck")
async def start_precheck(payload: dict[str, Any]):
    ids = _resolve_ids(payload)
    if not ids:
        raise HTTPException(400, "No URLs selected.")
    job_id = _create_job("precheck", len(ids))
    task = asyncio.create_task(_run_precheck(job_id, ids))
    _jobs[job_id] = task
    return {"job_id": job_id, "total": len(ids)}


@app.post("/api/urls/indexnow")
async def start_indexnow(payload: dict[str, Any], request: Request):
    ids = _resolve_ids(payload)
    if not ids:
        raise HTTPException(400, "No URLs selected.")
    job_id = _create_job("indexnow", len(ids))
    task = asyncio.create_task(_run_indexnow(job_id, ids, public_base(request)))
    _jobs[job_id] = task
    return {"job_id": job_id, "total": len(ids)}


@app.post("/api/urls/google-index")
async def start_google_index(payload: dict[str, Any]):
    ids = _resolve_ids(payload)
    if not ids:
        raise HTTPException(400, "No URLs selected.")
    if not google_indexing.service_account_present():
        raise HTTPException(
            400,
            "Upload a Google service-account JSON in Settings first. "
            "This API only works for your own Search Console–verified site.",
        )
    job_id = _create_job("google-index", len(ids))
    task = asyncio.create_task(_run_google_index(job_id, ids))
    _jobs[job_id] = task
    return {"job_id": job_id, "total": len(ids)}


@app.post("/api/urls/check-indexed")
async def start_check_indexed(payload: dict[str, Any]):
    ids = _resolve_ids(payload)
    if not ids:
        raise HTTPException(400, "No URLs selected.")
    settings = get_settings()
    if not settings.get("serp_api_key") and (settings.get("serp_provider") or "") != "custom":
        raise HTTPException(
            400,
            "No SERP API key is configured. Add one in Settings, or use the manual 'Mark indexed' toggle. "
            "This tool will not invent index status.",
        )
    job_id = _create_job("check-indexed", len(ids))
    task = asyncio.create_task(_run_check_indexed(job_id, ids))
    _jobs[job_id] = task
    return {"job_id": job_id, "total": len(ids)}


@app.post("/api/hub/ping")
async def ping_hub(request: Request):
    hub = hub_urls(request)
    if not hub["base"]:
        raise HTTPException(400, "Set a public base URL in Settings before pinging.")
    http = client()
    hub_endpoint = get_setting("websub_hub") or websub.DEFAULT_HUB
    websub_result = await websub.ping_websub(http, hub["rss"], hub_endpoint)
    bing_result = await websub.ping_bing_sitemap(http, hub["sitemap"])
    now = utcnow()
    with tx() as conn:
        conn.execute(
            """INSERT INTO submission_log (url_id, method, status, detail, created_at)
               VALUES (NULL, ?, ?, ?, ?)""",
            (
                "hub-ping",
                "ok" if (websub_result.get("ok") or bing_result.get("ok")) else "error",
                json.dumps({"websub": websub_result, "bing": bing_result}),
                now,
            ),
        )
        # Mark hub-listed URLs as submitted via hub if still pending.
        conn.execute(
            """UPDATE urls SET submission_status = CASE
                    WHEN submission_status IN ('pending', '') THEN 'submitted_hub'
                    ELSE submission_status
               END,
               last_submitted_at = COALESCE(last_submitted_at, ?),
               updated_at = ?
               WHERE in_hub = 1""",
            (now, now),
        )
    return {
        "websub": websub_result,
        "bing_sitemap_ping": bing_result,
        "google_sitemap_ping": {
            "ok": False,
            "detail": "Google's sitemap ping endpoint is deprecated and is not called.",
        },
        "hub": hub,
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    row = get_conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    return {"job": row_to_dict(row)}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def read_settings(request: Request):
    data = get_settings()
    # Never echo secrets in full if we want... actually this is self-hosted and
    # the settings page needs to show whether a key is set. Mask API keys.
    masked = dict(data)
    if masked.get("serp_api_key"):
        masked["serp_api_key_set"] = True
        masked["serp_api_key"] = _mask(masked["serp_api_key"])
    else:
        masked["serp_api_key_set"] = False
    masked["google_sa_present"] = google_indexing.service_account_present()
    if google_indexing.service_account_present():
        try:
            info = json.loads(SA_PATH.read_text())
            masked["google_sa_email"] = info.get("client_email", "")
        except Exception:
            masked["google_sa_email"] = ""
    else:
        masked["google_sa_email"] = ""
    masked["indexnow_key_url"] = (
        indexnow.key_file_url(public_base(request), data.get("indexnow_key") or "")
        if data.get("indexnow_key")
        else ""
    )
    hosts = get_conn().execute("SELECT * FROM owned_hosts ORDER BY host").fetchall()
    return {"settings": masked, "owned_hosts": rows_to_dicts(hosts), "hub": hub_urls(request)}


@app.post("/api/settings")
async def write_settings(payload: dict[str, Any], request: Request):
    allowed = {
        "public_base_url",
        "indexnow_key",
        "serp_provider",
        "serp_api_key",
        "serp_endpoint",
        "websub_hub",
        "hub_include",
        "default_target_domain",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    if "serp_api_key" in updates and str(updates["serp_api_key"]).endswith("••••"):
        # Masked value sent back unchanged — ignore.
        updates.pop("serp_api_key")
    if "serp_api_key" in updates and str(updates["serp_api_key"]).strip() == "":
        updates["serp_api_key"] = ""
    if "public_base_url" in updates:
        updates["public_base_url"] = str(updates["public_base_url"] or "").rstrip("/")
    if "default_target_domain" in updates:
        updates["default_target_domain"] = registrable_host(str(updates["default_target_domain"]).strip()) or DEFAULT_TARGET_DOMAIN
    if "indexnow_key" in updates:
        key = str(updates["indexnow_key"]).strip()
        if key and (len(key) < 8 or len(key) > 128):
            raise HTTPException(400, "IndexNow key must be 8–128 characters.")
        updates["indexnow_key"] = key
    set_settings(updates)
    return await read_settings(request)


@app.post("/api/settings/indexnow/generate")
async def generate_indexnow_key(request: Request):
    key = indexnow.generate_key()
    set_setting("indexnow_key", key)
    return await read_settings(request)


@app.post("/api/settings/owned-hosts")
async def add_owned_host(payload: dict[str, Any]):
    host = registrable_host((payload.get("host") or "").strip())
    if not host:
        raise HTTPException(400, "Enter a hostname such as example.com.")
    notes = (payload.get("notes") or "").strip()
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO owned_hosts (host, notes, created_at) VALUES (?, ?, ?)",
            (host, notes, utcnow()),
        )
        if notes:
            conn.execute("UPDATE owned_hosts SET notes=? WHERE host=?", (notes, host))
    hosts = get_conn().execute("SELECT * FROM owned_hosts ORDER BY host").fetchall()
    return {"owned_hosts": rows_to_dicts(hosts)}


@app.delete("/api/settings/owned-hosts/{host_id}")
async def delete_owned_host(host_id: int):
    with tx() as conn:
        conn.execute("DELETE FROM owned_hosts WHERE id=?", (host_id,))
    return {"ok": True}


@app.post("/api/settings/google-sa")
async def upload_google_sa(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        info = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "File is not valid JSON.")
    if info.get("type") != "service_account" or "private_key" not in info or "client_email" not in info:
        raise HTTPException(400, "This does not look like a Google service-account JSON key.")
    SA_PATH.write_text(json.dumps(info, indent=2))
    try:
        SA_PATH.chmod(0o600)
    except Exception:
        pass
    return {"ok": True, "client_email": info.get("client_email")}


@app.delete("/api/settings/google-sa")
async def delete_google_sa():
    if SA_PATH.exists():
        SA_PATH.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_project(project_id: int) -> Optional[dict]:
    return row_to_dict(
        get_conn().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    )


def _filter_clause(filter_name: str, alias: str = "") -> str:
    a = alias
    mapping = {
        "live": f"{a}is_live = 1",
        "dead": f"{a}is_live = 0",
        "has_link": f"{a}has_link = 1",
        "no_link": f"{a}has_link = 0 AND {a}last_checked_at IS NOT NULL",
        "blocked": f"({a}noindex = 1 OR {a}robots_disallow = 1 OR {a}canonicalized_away = 1)",
        "noindex": f"{a}noindex = 1",
        "robots": f"{a}robots_disallow = 1",
        "canonical": f"{a}canonicalized_away = 1",
        "indexed": f"{a}index_status IN ('indexed', 'manual_indexed')",
        "not_indexed": f"{a}index_status IN ('not_indexed', 'manual_not_indexed')",
        "unknown_index": f"{a}index_status = 'unknown'",
        "unchecked": f"{a}last_checked_at IS NULL",
        "pending": f"{a}submission_status = 'pending'",
        "in_hub": f"{a}in_hub = 1",
    }
    return mapping.get(filter_name, "")


def _parse_upload(text: str, filename: str) -> list[str]:
    lines: list[str] = []
    lower = filename.lower()
    if lower.endswith(".csv"):
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []
        header = [c.strip().lower() for c in rows[0]]
        url_idx = 0
        for candidate in ("url", "urls", "link", "links", "page", "href"):
            if candidate in header:
                url_idx = header.index(candidate)
                rows = rows[1:]
                break
        else:
            # If the first cell looks like a header, skip it.
            if header and not normalize_submitted(rows[0][0] if rows[0] else ""):
                rows = rows[1:]
        for row in rows:
            if row and url_idx < len(row):
                lines.append(row[url_idx])
        return lines
    return text.splitlines()


def _ingest_urls(project_id: int, raw_lines: list[str], batch_name: str) -> dict:
    seen: set[str] = set()
    urls: list[str] = []
    skipped = 0
    for line in raw_lines:
        # CSV-ish lines pasted in the textarea: take the first column.
        piece = line.split(",")[0] if "," in line and not line.strip().startswith("http") else line
        url = normalize_submitted(piece)
        if not url:
            if line.strip():
                skipped += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        return {"added": 0, "duplicates": 0, "skipped": skipped, "batch_id": None, "total_in_batch": 0}
    now = utcnow()
    added = 0
    duplicates = 0
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO batches (project_id, name, created_at) VALUES (?, ?, ?)",
            (project_id, batch_name, now),
        )
        batch_id = cur.lastrowid
        for url in urls:
            try:
                conn.execute(
                    """INSERT INTO urls (project_id, batch_id, url, in_hub, submission_status,
                       index_status, created_at, updated_at)
                       VALUES (?, ?, ?, 1, 'pending', 'unknown', ?, ?)""",
                    (project_id, batch_id, url, now, now),
                )
                added += 1
            except Exception:
                duplicates += 1
                # Still associate? leave the existing row as-is.
    return {
        "added": added,
        "duplicates": duplicates,
        "skipped": skipped,
        "batch_id": batch_id,
        "total_in_batch": added,
    }


def _hub_url_rows() -> list[dict]:
    include = get_setting("hub_include") or "all"
    extra = ""
    if include == "live":
        extra = "AND is_live = 1"
    elif include == "has_link":
        extra = "AND has_link = 1"
    rows = get_conn().execute(
        f"""SELECT url, updated_at, created_at, has_link, is_live, http_status
            FROM urls
            WHERE in_hub = 1 {extra}
            ORDER BY id DESC"""
    ).fetchall()
    return rows_to_dicts(rows)


def _rss_date(iso: Optional[str]) -> str:
    if not iso:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "••••••••"
    return value[:3] + "••••" + value[-2:]


def _resolve_ids(payload: dict[str, Any]) -> list[int]:
    if payload.get("ids"):
        return [int(i) for i in payload["ids"]]
    project_id = payload.get("project_id")
    if not project_id:
        return []
    where = ["project_id = ?"]
    params: list[Any] = [int(project_id)]
    if payload.get("batch_id"):
        where.append("batch_id = ?")
        params.append(int(payload["batch_id"]))
    if payload.get("filter") and payload["filter"] != "all":
        clause = _filter_clause(payload["filter"])
        if clause:
            where.append(clause)
    if payload.get("q"):
        where.append("url LIKE ?")
        params.append(f"%{payload['q']}%")
    rows = get_conn().execute(
        f"SELECT id FROM urls WHERE {' AND '.join(where)}", params
    ).fetchall()
    return [r["id"] for r in rows]


def _create_job(job_type: str, total: int) -> str:
    job_id = uuid.uuid4().hex
    with tx() as conn:
        conn.execute(
            """INSERT INTO jobs (id, type, status, total, done_count, ok_count, failed_count, message, created_at)
               VALUES (?, ?, 'running', ?, 0, 0, 0, '', ?)""",
            (job_id, job_type, total, utcnow()),
        )
    return job_id


def _update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*fields.values(), job_id])


def _owned_host_set() -> set[str]:
    rows = get_conn().execute("SELECT host FROM owned_hosts").fetchall()
    hosts = {registrable_host(r["host"]) for r in rows}
    # The public hub host is always "owned" for IndexNow of hub pages.
    base = get_setting("public_base_url") or PUBLIC_BASE_URL
    if base:
        hosts.add(registrable_host(host_of(base)))
    return {h for h in hosts if h}


def _is_owned_url(url: str, owned: set[str]) -> bool:
    host = registrable_host(host_of(url))
    if host in owned:
        return True
    for h in owned:
        if host == h or host.endswith("." + h):
            return True
    return False


async def _run_precheck(job_id: str, ids: list[int]) -> None:
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    robots_cache = RobotsCache()
    ok = failed = 0
    done = 0
    http = client()

    async def one(url_id: int) -> None:
        nonlocal ok, failed, done
        row = get_conn().execute(
            """SELECT u.*, p.target_domain, p.include_subdomains
               FROM urls u JOIN projects p ON p.id = u.project_id
               WHERE u.id=?""",
            (url_id,),
        ).fetchone()
        if not row:
            done += 1
            failed += 1
            return
        async with sem:
            result = await precheck_url(
                http,
                row["url"],
                row["target_domain"],
                bool(row["include_subdomains"]),
                robots_cache,
            )
        now = utcnow()
        with tx() as conn:
            conn.execute(
                """UPDATE urls SET
                    final_url=?, http_status=?, is_live=?, fetch_error=?, content_type=?,
                    meta_robots=?, x_robots_tag=?, noindex=?, robots_disallow=?,
                    canonical=?, canonicalized_away=?, has_link=?, link_rel=?,
                    link_count=?, link_details=?, last_checked_at=?, updated_at=?
                   WHERE id=?""",
                (
                    result.get("final_url"),
                    result.get("http_status"),
                    1 if result.get("is_live") else 0 if result.get("is_live") is not None else None,
                    result.get("fetch_error"),
                    result.get("content_type"),
                    result.get("meta_robots"),
                    result.get("x_robots_tag"),
                    1 if result.get("noindex") else 0,
                    None if result.get("robots_disallow") is None else (1 if result.get("robots_disallow") else 0),
                    result.get("canonical"),
                    1 if result.get("canonicalized_away") else 0,
                    1 if result.get("has_link") else 0,
                    result.get("link_rel") or "none",
                    result.get("link_count") or 0,
                    result.get("link_details_json") or "[]",
                    now,
                    now,
                    url_id,
                ),
            )
        done += 1
        if result.get("fetch_error") and not result.get("http_status"):
            failed += 1
        else:
            ok += 1
        _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed)

    try:
        await asyncio.gather(*[one(i) for i in ids])
        _update_job(
            job_id,
            status="done",
            done_count=done,
            ok_count=ok,
            failed_count=failed,
            finished_at=utcnow(),
            message=f"Checked {done} URLs ({ok} fetched, {failed} failed).",
        )
    except Exception as exc:
        _update_job(job_id, status="error", message=str(exc), finished_at=utcnow())


async def _run_indexnow(job_id: str, ids: list[int], base: str) -> None:
    key = get_setting("indexnow_key")
    if not key:
        _update_job(
            job_id,
            status="error",
            message="Generate or paste an IndexNow key in Settings first.",
            finished_at=utcnow(),
        )
        return
    owned = _owned_host_set()
    rows = get_conn().execute(
        f"SELECT * FROM urls WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    eligible = []
    skipped = []
    for r in rows:
        if _is_owned_url(r["url"], owned):
            eligible.append(r)
        else:
            skipped.append(r)
    # Always consider the public hub documents — those are the pages the user
    # actually controls. Skip loopback bases; IndexNow cannot verify them.
    hub_targets = []
    base_host = registrable_host(host_of(base)) if base else ""
    if base and base_host and base_host not in {"127.0.0.1", "localhost", "::1"}:
        hub_targets = [
            f"{base}/hub",
            f"{base}/hub/sitemap.xml",
            f"{base}/hub/feed.xml",
            f"{base}/hub/atom.xml",
        ]

    now = utcnow()
    ok = failed = 0
    details = []
    http = client()

    groups = indexnow.hosts_for_urls([r["url"] for r in eligible] + hub_targets)
    done = 0
    total_attempted = sum(len(v) for v in groups.values())
    _update_job(job_id, total=max(total_attempted, 1), message="Submitting owned URLs via IndexNow…")

    if not groups:
        msg = (
            "IndexNow only accepts URLs on hosts where you host the key file. "
            "None of the selected URLs are on an owned host or your public hub domain. "
            f"Skipped {len(skipped)} third-party backlink URL(s)."
        )
        _update_job(job_id, status="done", message=msg, done_count=0, finished_at=now)
        return

    for host, url_list in groups.items():
        key_location = f"https://{host}/{key}.txt"
        # If this is the hub host and public_base_url is set, use that exact key location.
        if base and registrable_host(host_of(base)) == registrable_host(host):
            key_location = indexnow.key_file_url(base, key)
        result = await indexnow.submit(http, url_list, key, key_location)
        details.append(result)
        status = "ok" if result.get("ok") else "error"
        if result.get("ok"):
            ok += len(url_list)
        else:
            failed += len(url_list)
        done += len(url_list)
        with tx() as conn:
            for url in url_list:
                conn.execute(
                    """UPDATE urls SET indexnow_status=?, last_submitted_at=?, updated_at=?,
                       submission_status = CASE
                         WHEN submission_status IN ('pending', 'submitted_hub') THEN 'submitted_indexnow'
                         ELSE submission_status
                       END
                       WHERE url=?""",
                    (result.get("detail"), now, now, url),
                )
                conn.execute(
                    """INSERT INTO submission_log (url_id, method, status, detail, created_at)
                       SELECT id, 'indexnow', ?, ?, ? FROM urls WHERE url=?""",
                    (status, result.get("detail"), now, url),
                )
        _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed)

    skipped_note = ""
    if skipped:
        skipped_note = (
            f" Skipped {len(skipped)} third-party URL(s) — IndexNow cannot submit pages "
            "on hosts you do not control. Add the host under Settings → Owned hosts "
            "only if you actually control it and can serve the key file there."
        )
    _update_job(
        job_id,
        status="done",
        finished_at=utcnow(),
        message=f"IndexNow submitted {ok} URL(s), {failed} failed.{skipped_note}",
    )


async def _run_google_index(job_id: str, ids: list[int]) -> None:
    rows = get_conn().execute(
        f"SELECT * FROM urls WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    owned = _owned_host_set()
    ok = failed = done = 0
    skipped = 0
    # The Google API client is synchronous; run in a thread to avoid blocking.
    loop = asyncio.get_event_loop()
    for r in rows:
        if owned and not _is_owned_url(r["url"], owned):
            # Still attempt if user explicitly selected it, but warn — the API
            # will reject third-party URLs. We skip unless the host is marked owned
            # OR owned list is empty (then we try and let Google reject).
            skipped += 1
            done += 1
            with tx() as conn:
                conn.execute(
                    "UPDATE urls SET google_index_status=?, updated_at=? WHERE id=?",
                    (
                        "Skipped: host is not in Owned hosts. The Google Indexing API is own-site only.",
                        utcnow(),
                        r["id"],
                    ),
                )
            _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed)
            continue
        result = await loop.run_in_executor(None, google_indexing.publish_url, r["url"])
        now = utcnow()
        status = "ok" if result.get("ok") else "error"
        detail = result.get("detail") or ""
        if result.get("ok"):
            ok += 1
        else:
            failed += 1
        done += 1
        with tx() as conn:
            conn.execute(
                """UPDATE urls SET google_index_status=?, last_submitted_at=?, updated_at=?,
                   submission_status = CASE
                     WHEN ? = 'ok' THEN 'submitted_google'
                     ELSE submission_status
                   END
                   WHERE id=?""",
                (detail, now, now, status, r["id"]),
            )
            conn.execute(
                """INSERT INTO submission_log (url_id, method, status, detail, created_at)
                   VALUES (?, 'google-indexing-api', ?, ?, ?)""",
                (r["id"], status, detail, now),
            )
        _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed)
        await asyncio.sleep(0.2)
    _update_job(
        job_id,
        status="done",
        finished_at=utcnow(),
        message=(
            f"Google Indexing API: {ok} submitted, {failed} failed, {skipped} skipped "
            "(own verified site only)."
        ),
    )


async def _run_check_indexed(job_id: str, ids: list[int]) -> None:
    settings = get_settings()
    provider = settings.get("serp_provider") or "serpapi"
    api_key = settings.get("serp_api_key") or ""
    endpoint = settings.get("serp_endpoint") or ""
    rows = get_conn().execute(
        f"SELECT * FROM urls WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    ok = failed = done = 0
    http = client()
    for r in rows:
        result = await serp.check_indexed(http, r["url"], provider, api_key, endpoint)
        now = utcnow()
        if not result.get("ok") or result.get("indexed") is None:
            failed += 1
            done += 1
            with tx() as conn:
                conn.execute(
                    "UPDATE urls SET index_note=?, last_index_check_at=?, updated_at=? WHERE id=?",
                    (result.get("detail") or "SERP check failed", now, now, r["id"]),
                )
            _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed, message=result.get("detail"))
            continue
        indexed = bool(result["indexed"])
        status = "indexed" if indexed else "not_indexed"
        note = result.get("detail") or ""
        hits = result.get("hits") or []
        if hits:
            note += " Hits: " + ", ".join(hits[:3])
        with tx() as conn:
            conn.execute(
                """UPDATE urls SET index_status=?, index_source=?, index_note=?,
                   last_index_check_at=?, updated_at=? WHERE id=?""",
                (status, provider, note, now, now, r["id"]),
            )
        ok += 1
        done += 1
        _update_job(job_id, done_count=done, ok_count=ok, failed_count=failed)
        await asyncio.sleep(0.3)
    _update_job(
        job_id,
        status="done",
        finished_at=utcnow(),
        message=f"Index checks finished: {ok} ok, {failed} failed. Status is never invented.",
    )


if __name__ == "__main__":
    import uvicorn
    from .config import HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
