"""Routes added by the indexer network: crons, hub pages, connections, tools, MCP."""

from __future__ import annotations

import json
from typing import Any, Optional
import httpx
from fastapi import Form, HTTPException, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from . import network
from .credits import summary_for_user
from .database import get_conn, get_setting, utcnow
from .engine import (
    GOOGLE_PUSH_WARNING,
    campaign_report,
    check_loose,
    decorate_url,
    finalize_intake,
    googlebot_stats,
    list_campaign_urls,
    recheck_url,
    run_drip,
    run_inspection,
    run_precheck,
    run_quota_reset,
    run_serp_checks,
    serp_remaining,
)
from .services import gsc, indexnow
from .services.urlutil import is_safe_public_url, registrable_host
from .ui import login_redirect, render, templates

GOOGLE_WARNING = GOOGLE_PUSH_WARNING


def _need(request: Request):
    from .main import _user

    user = _user(request)
    if not user:
        return None, login_redirect(request)
    return user, None


def _notice(path: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}notice={message}", status_code=303)


def _cron_ok(request: Request) -> bool:
    from .main import _cron_ok as ok

    return ok(request)


def render_domain_home(request: Request, domain: dict):
    pages = network.pages_for_domain(domain["id"])
    return templates.TemplateResponse(
        request,
        "hub_domain.html",
        {
            "request": request,
            "domain": domain,
            "pages": pages,
            "host": domain["hostname"],
        },
    )


def register(app) -> None:
    @app.get("/l/{slug}")
    async def hub_link_page(slug: str, request: Request):
        domain = getattr(request.state, "hub_domain", None)
        page = network.page_by_slug(domain["id"] if domain else None, slug)
        if not page:
            raise HTTPException(404, "Unknown hub page")
        try:
            ip = network.client_ip(dict(request.headers), request.client.host if request.client else "")
            network.record_bot_hit(int(page["id"]), request.headers.get("user-agent") or "", ip)
        except Exception:
            pass
        from .crawlhub import crawl_base

        base = f"https://{domain['hostname']}" if domain else crawl_base(str(request.base_url))
        prev_url, next_url = network.neighbor_urls(domain, page, base)
        fresh = network.page_by_id(int(page["id"])) or page
        return templates.TemplateResponse(
            request,
            "hub_links.html",
            {
                "request": request,
                "domain": domain,
                "page": fresh,
                "links": network.urls_on_page(fresh),
                "canonical": network.page_http_url(domain, fresh["slug"], base),
                "prev_url": prev_url,
                "next_url": next_url,
                "host": domain["hostname"] if domain else "this site",
                "updated": (fresh.get("last_pushed_at") or fresh.get("created_at") or "")[:10],
            },
        )

    @app.get("/app/settings/network")
    async def network_page(request: Request):
        user, redirect = _need(request)
        if redirect:
            return redirect
        if not user.get("is_admin"):
            return _notice("/app/settings", "Indexer Network is limited to the operator account.")
        domains = []
        for domain in network.list_domains():
            view = dict(domain)
            view["emails"] = network.gcp_emails(domain)
            view["quota"] = network.quota_snapshot(domain)
            domains.append(view)
        return render(
            request,
            "network.html",
            user=user,
            active="network",
            domains=domains,
            events=network.recent_events(30),
            warning=GOOGLE_WARNING,
        )

    @app.post("/app/settings/network")
    async def network_save(
        request: Request,
        action: str = Form("add"),
        hostname: str = Form(""),
        domain_id: int = Form(0),
        google_push: str = Form(""),
        file: Optional[UploadFile] = File(None),
    ):
        user, redirect = _need(request)
        if redirect:
            return redirect
        if not user.get("is_admin"):
            return _notice("/app/settings", "Indexer Network is limited to the operator account.")
        if action == "add":
            domain, error = network.add_domain(hostname, google_push == "1")
            if error or not domain:
                return _notice("/app/settings/network", error or "Could not add that domain.")
            if file is not None and file.filename:
                raw = await file.read()
                try:
                    info = json.loads(raw.decode("utf-8"))
                except Exception:
                    return _notice("/app/settings/network", f"Added {domain['hostname']}, but the JSON was not valid.")
                message = network.add_gcp_project(domain["id"], info)
                if message:
                    return _notice("/app/settings/network", f"Added {domain['hostname']}. {message}")
            return _notice(
                "/app/settings/network",
                f"Added {domain['hostname']}. Point DNS at this app and host /{domain['indexnow_key']}.txt "
                "(this app serves it automatically on that hostname).",
            )
        if action == "push" and domain_id:
            network.set_google_push(domain_id, google_push == "1")
            return _notice("/app/settings/network", "Google push setting saved.")
        if action == "gcp" and domain_id and file is not None:
            raw = await file.read()
            try:
                info = json.loads(raw.decode("utf-8"))
            except Exception:
                return _notice("/app/settings/network", "That file is not valid JSON.")
            message = network.add_gcp_project(domain_id, info)
            return _notice("/app/settings/network", message or "Google Cloud project saved.")
        if action == "verify" and domain_id:
            domain = network.domain_by_id(domain_id)
            if not domain:
                return _notice("/app/settings/network", "Hub domain not found.")
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                result = await network.verify_indexnow(client, domain)
            return _notice("/app/settings/network", result.get("detail") or "Checked the IndexNow key.")
        if action == "delete" and domain_id:
            network.delete_domain(domain_id)
            return _notice("/app/settings/network", "Hub domain removed. Existing pages stay in the database.")
        return _notice("/app/settings/network", "Nothing to change.")

    @app.get("/app/settings/connections")
    async def connections_page(request: Request):
        user, redirect = _need(request)
        if redirect:
            return redirect
        config = gsc.load_config(user["id"], "gsc")
        hosts = gsc.indexnow_hosts(user["id"])
        left, limit = serp_remaining()
        return render(
            request,
            "connections.html",
            user=user,
            active="connections",
            properties=config.get("properties") or [],
            connected=bool(config.get("access_token")),
            hosts=hosts,
            serp_left=left,
            serp_limit=limit,
            google_client=bool(__import__("os").environ.get("GOOGLE_CLIENT_ID")),
        )

    @app.get("/app/connections/google/start")
    async def google_start(request: Request):
        user, redirect = _need(request)
        if redirect:
            return redirect
        redirect_uri = str(request.base_url).rstrip("/") + "/app/connections/google/callback"
        url = gsc.oauth_start_url(user["id"], redirect_uri)
        if not url:
            return _notice(
                "/app/settings/connections",
                "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and ENCRYPTION_KEY before connecting Search Console.",
            )
        return RedirectResponse(url, status_code=303)

    @app.get("/app/connections/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        user, redirect = _need(request)
        if redirect:
            return redirect
        if error:
            return _notice("/app/settings/connections", "Google did not grant access.")
        user_id = gsc.read_oauth_state(state)
        if user_id != int(user["id"]):
            return _notice("/app/settings/connections", "That Google connection link has expired. Try again.")
        redirect_uri = str(request.base_url).rstrip("/") + "/app/connections/google/callback"
        async with httpx.AsyncClient(timeout=30) as client:
            traded = await gsc.exchange_code(client, code, redirect_uri)
            if not traded.get("ok"):
                return _notice("/app/settings/connections", traded.get("detail") or "Google token exchange failed.")
            token = traded["token"]
            properties = await gsc.fetch_properties(client, {"access_token": token.get("access_token")})
        gsc.store_oauth(user["id"], token, properties)
        return _notice("/app/settings/connections", f"Search Console connected. {len(properties)} verified properties.")

    @app.post("/app/settings/connections/indexnow")
    async def connection_indexnow(
        request: Request,
        action: str = Form("generate"),
        host: str = Form(""),
    ):
        user, redirect = _need(request)
        if redirect:
            return redirect
        name = registrable_host(host) or (host or "").strip().lower()
        if not name or "." not in name:
            return _notice("/app/settings/connections", "Enter the hostname you verified.")
        if action == "generate":
            gsc.save_indexnow_host(user["id"], name, indexnow.generate_key(), "")
            return _notice("/app/settings/connections", f"IndexNow key saved for {name}. Host the key file, then verify it.")
        current = gsc.indexnow_for(user["id"], name)
        if not current.get("key"):
            return _notice("/app/settings/connections", "Generate a key first.")
        url = f"https://{name}/{current['key']}.txt"
        safe, reason = is_safe_public_url(url)
        if not safe:
            return _notice("/app/settings/connections", reason)
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            try:
                response = await client.get(url)
            except Exception as exc:
                return _notice("/app/settings/connections", f"Could not reach the key file: {exc}")
        if response.status_code == 200 and (response.text or "").strip() == current["key"]:
            gsc.save_indexnow_host(user["id"], name, current["key"], utcnow())
            return _notice("/app/settings/connections", f"IndexNow key verified on {name}.")
        return _notice("/app/settings/connections", f"HTTP {response.status_code}. The file must contain only the key.")

    @app.post("/app/settings/connections/sitemap")
    async def connection_sitemap(
        request: Request,
        site_url: str = Form(""),
        feed_url: str = Form(""),
    ):
        user, redirect = _need(request)
        if redirect:
            return redirect
        async with httpx.AsyncClient(timeout=30) as client:
            config = await gsc.authorized_config(client, user["id"])
            result = await gsc.submit_sitemap(client, config, site_url.strip(), feed_url.strip())
        return _notice("/app/settings/connections", result.get("detail") or "Sitemap resubmit finished.")

    @app.post("/app/tools/check")
    async def tools_check(request: Request, urls: str = Form("")):
        user, redirect = _need(request)
        if redirect:
            return redirect
        lines = [line.strip() for line in (urls or "").splitlines() if line.strip()]
        if not lines:
            return _notice("/app/tools", "Paste at least one URL.")
        report = await check_loose(user, lines)
        left, limit = serp_remaining()
        return render(
            request,
            "tools.html",
            user=user,
            active="tools",
            hub=_hub(request),
            checker=report,
            serp_left=left,
            serp_limit=limit,
            pasted=urls,
        )

    @app.api_route("/api/cron/precheck", methods=["GET", "POST"])
    async def cron_precheck(request: Request):
        if not _cron_ok(request):
            raise HTTPException(401, "Cron authentication failed.")
        return {"ok": True, **(await run_precheck())}

    @app.api_route("/api/cron/drip", methods=["GET", "POST"])
    async def cron_drip(request: Request):
        if not _cron_ok(request):
            raise HTTPException(401, "Cron authentication failed.")
        return {"ok": True, **(await run_drip())}

    @app.api_route("/api/cron/serp-check", methods=["GET", "POST"])
    async def cron_serp(request: Request):
        if not _cron_ok(request):
            raise HTTPException(401, "Cron authentication failed.")
        return {"ok": True, **(await run_serp_checks())}

    @app.api_route("/api/cron/inspection-poll", methods=["GET", "POST"])
    async def cron_inspection(request: Request):
        if not _cron_ok(request):
            raise HTTPException(401, "Cron authentication failed.")
        return {"ok": True, **(await run_inspection())}

    @app.api_route("/api/cron/quota-reset", methods=["GET", "POST"])
    async def cron_quota(request: Request):
        if not _cron_ok(request):
            raise HTTPException(401, "Cron authentication failed.")
        return {"ok": True, **run_quota_reset()}

    @app.get("/api/v1/campaigns/{project_id}/urls")
    async def v1_url_list(project_id: int, request: Request, status: str = "", page: int = 1, per: int = 50):
        from .main import _bearer_user

        user = _bearer_user(request)
        if not user:
            raise HTTPException(401, "Send Authorization: Bearer <api key>.")
        from .projects import get_project

        project = get_project(project_id, user["id"])
        if not project:
            raise HTTPException(404, "Project not found")
        from .crawlhub import crawl_base

        return list_campaign_urls(project_id, status, page, per, crawl_base(str(request.base_url)))

    @app.post("/api/v1/urls/{url_id}/recheck")
    async def v1_recheck(url_id: int, request: Request):
        from .main import _bearer_user

        user = _bearer_user(request)
        if not user:
            raise HTTPException(401, "Send Authorization: Bearer <api key>.")
        status, payload = await recheck_url(int(user["id"]), url_id)
        if status != 200:
            raise HTTPException(status, payload.get("detail") or "Recheck failed.")
        return payload

    @app.post("/api/mcp")
    async def mcp_endpoint(request: Request):
        from .main import _bearer_user

        user = _bearer_user(request)
        if not user:
            raise HTTPException(401, "Send Authorization: Bearer <api key>.")
        try:
            body = await request.json()
        except Exception:
            body = {}
        return await _mcp_call(user, body, request)


def _hub(request: Request) -> dict[str, str]:
    from .main import hub_urls

    return hub_urls(request)


_MCP_TOOLS = [
    {
        "name": "get_credits",
        "description": "Credit balance, reserved credits, and refunded URL counts. Indexed means SERP-confirmed or manually marked.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_campaign",
        "description": "Create an indexing campaign and reserve credits for the URL list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "project_type": {"type": "string"},
                "daily_speed": {"type": "integer"},
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_campaign",
        "description": "Campaign counts, indexed rate, and refunded URLs. Indexed counts are confirmed only.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "list_urls",
        "description": "Paginated URL rows for a campaign.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "status": {"type": "string"},
                "page": {"type": "integer"},
            },
            "required": ["id"],
        },
    },
]


async def _mcp_call(user: dict, body: dict, request: Request) -> dict[str, Any]:
    method = body.get("method") or ""
    rpc_id = body.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "backlink-indexer", "version": "1.4.2"}, "capabilities": {"tools": {}}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": _MCP_TOOLS}}
    if method != "tools/call":
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not found"}}
    params = body.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        result = await _mcp_tool(user, name, args, request)
    except HTTPException as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": exc.status_code, "message": str(exc.detail)}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}}


async def _mcp_tool(user: dict, name: str, args: dict, request: Request) -> dict:
    from .projects import create_project, get_project

    if name == "get_credits":
        return _credit_payload(user)
    if name == "create_campaign":
        project, error = create_project(
            user,
            name=str(args.get("name") or ""),
            target_domain="",
            project_type=str(args.get("project_type") or "indexing"),
            daily_speed=int(args.get("daily_speed") or 10),
        )
        if not project:
            raise HTTPException(400, error)
        from .ingest import add_urls

        lines = args.get("urls") or []
        result = add_urls(user, project, [str(item) for item in lines], "MCP batch") if lines else {"added": 0, "accepted": [], "not_sendable": [], "skipped_duplicates": []}
        result = await finalize_intake(result)
        return {
            "project": get_project(project["id"], user["id"]),
            "accepted": result.get("accepted") or [],
            "not_sendable": result.get("not_sendable") or [],
            "skipped_duplicates": result.get("skipped_duplicates") or [],
        }
    if name == "get_campaign":
        project = get_project(int(args.get("id") or 0), user["id"])
        if not project:
            raise HTTPException(404, "Project not found")
        return {"project": project, **campaign_report(project["id"])}
    if name == "list_urls":
        project = get_project(int(args.get("id") or 0), user["id"])
        if not project:
            raise HTTPException(404, "Project not found")
        from .crawlhub import crawl_base

        return list_campaign_urls(project["id"], str(args.get("status") or ""), int(args.get("page") or 1), 50, crawl_base(str(request.base_url)))
    raise HTTPException(400, "Unknown tool.")


def _credit_payload(user: dict) -> dict[str, Any]:
    summary = summary_for_user(int(user["id"]))
    payload = {
        "refunded_urls": summary["refunded_urls"],
        "refunded_credits": summary["refunded_credits"],
        "reserved_credits": summary["reserved_credits"],
        "reserved_urls": summary["reserved_urls"],
        "consumed_credits": summary["consumed_credits"],
        "refund_window_days": int(get_setting("refund_window_days") or "7"),
        "note": "Credits are reserved at intake and consumed only when a URL is confirmed indexed. Reports count confirmed indexed URLs only. They never guess.",
    }
    if user.get("unlimited"):
        payload.update({"unlimited": True, "plan": "VIP", "token_balance": None, "daily_speed_cap": None})
    else:
        payload.update(
            {
                "unlimited": False,
                "token_balance": user["token_balance"],
                "daily_speed_cap": user["daily_speed_cap"],
                "plan": user["plan_name"],
            }
        )
    return payload
