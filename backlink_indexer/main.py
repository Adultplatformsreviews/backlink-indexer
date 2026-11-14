from __future__ import annotations

import csv
import hmac
import io
import json
import os
from typing import Any, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .activity import link_history, submission_chart
from .auth import (
    add_tokens,
    api_key_prefix,
    create_session,
    delete_session,
    get_user,
    revoke_api_key,
    rotate_api_key,
    user_from_api_key,
)
from .auth import (
    create_user,
    get_user_by_username,
    hash_password,
    list_users,
    set_user_disabled,
    set_user_password,
    verify_password,
)
from .limits import urls_used_today
from .email_login import consume_reset_token, issue_reset
from .mailer import describe_mailer
from .config import PUBLIC_BASE_URL, SA_PATH
from .database import (
    bootstrap_db,
    close_conn,
    get_conn,
    get_setting,
    get_settings,
    init_db,
    set_setting,
    set_settings,
    tx,
    utcnow,
)
from .ingest import add_urls, parse_upload, sitemap_locs
from .projects import (
    PROJECT_TYPES,
    TYPE_LABELS,
    active_count,
    allocated_speed,
    archive_count,
    create_project,
    delete_project,
    get_project,
    list_projects,
    list_urls,
    set_engines,
    set_run_status,
    set_speed,
    type_counts,
)
from .queue import check_ids, mark_engines, requeue, resubmit_failed, run_queue
from .services import google_indexing, indexnow, websub
from .services.secrets import setting_or_env
from .services.urlutil import host_of, registrable_host
from .ui import current_user, login_redirect, render

ROOT_DIR = __import__("pathlib").Path(__file__).resolve().parent

app = FastAPI(title="Backlink Indexer", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
_static_dir = ROOT_DIR / "static"
# check_dir=False so a missing bundle cannot crash process import.
# vercel.json includeFiles still ships static/ and templates/ with the function.
app.mount(
    "/static",
    StaticFiles(directory=str(_static_dir), check_dir=False),
    name="static",
)


@app.middleware("http")
async def close_db(request: Request, call_next):
    try:
        return await call_next(request)
    finally:
        close_conn()


@app.on_event("startup")
async def startup() -> None:
    # Database setup must not prevent the process from importing or serving /health.
    try:
        init_db()
        from .auth import promote_operator_accounts

        promote_operator_accounts()
    except Exception:
        pass
    try:
        close_conn()
    except Exception:
        pass


def _user(request: Request):
    return current_user(request)


def _need(request: Request):
    user = _user(request)
    if not user:
        return None, login_redirect(request)
    return user, None


def _session_response(response: RedirectResponse, request: Request, token: str) -> RedirectResponse:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        "bi_session",
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


def _safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/app"


def _notice(path: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}notice={message}", status_code=303)


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


def _hub_rows() -> list[dict]:
    include = get_setting("hub_include") or "all"
    extra = ""
    if include == "live":
        extra = "AND is_live = 1"
    elif include == "has_link":
        extra = "AND has_link = 1"
    return get_conn().execute(
        f"""SELECT url, updated_at, created_at, has_link, is_live, http_status
            FROM urls WHERE in_hub = 1 {extra} ORDER BY id DESC"""
    ).fetchall()


def _cron_ok(request: Request) -> bool:
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    header = request.headers.get("authorization") or ""
    if secret:
        return hmac.compare_digest(header, f"Bearer {secret}")
    if os.environ.get("VERCEL"):
        return (request.headers.get("user-agent") or "").lower().startswith("vercel-cron")
    return True


def _ids(form: Any) -> list[int]:
    values = form.getlist("ids") if hasattr(form, "getlist") else form.get("ids") or []
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


async def _lines_from_upload(upload: Optional[UploadFile], pasted: str) -> list[str]:
    lines = pasted.splitlines() if pasted else []
    if upload is not None and upload.filename:
        raw = await upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        lines.extend(parse_upload(text, upload.filename))
    return lines


async def _fetch_sitemap(url: str) -> tuple[list[str], str]:
    if not url:
        return [], ""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            response = await client.get(url)
    except Exception:
        return [], "Sitemap could not be fetched."
    if response.status_code != 200:
        return [], f"Sitemap returned HTTP {response.status_code}."
    locs = sitemap_locs(response.text)
    if not locs:
        return [], "Sitemap had no URLs."
    return locs, ""


ENGINE_FIELDS = (
    ("use_google", "Google Indexing API"),
    ("use_bing", "Bing Webmaster"),
    ("use_yandex", "Yandex Webmaster"),
    ("use_indexnow", "IndexNow"),
)


def _configured_map() -> dict[str, bool]:
    token, _token_source = setting_or_env("yandex_webmaster_token", "YANDEX_WEBMASTER_TOKEN")
    user_id, _user_source = setting_or_env("yandex_webmaster_user_id", "YANDEX_WEBMASTER_USER_ID")
    host_id, _host_source = setting_or_env("yandex_webmaster_host_id", "YANDEX_WEBMASTER_HOST_ID")
    return {
        "use_google": google_indexing.service_account_present(),
        "use_bing": bool(setting_or_env("bing_webmaster_api_key", "BING_WEBMASTER_API_KEY")[0]),
        "use_yandex": bool(token and user_id and host_id),
        "use_indexnow": bool(setting_or_env("indexnow_key", "INDEXNOW_KEY")[0]),
    }


def engine_choices(saved: Optional[dict] = None) -> list[dict]:
    configured = _configured_map()
    rows = []
    for key, label in ENGINE_FIELDS:
        if saved is None:
            checked = configured[key]
        else:
            checked = str(saved.get(key, 1)) not in {"0", "False", "false", ""}
        rows.append({"key": key, "label": label, "configured": configured[key], "checked": checked})
    return rows


def posted_engines(
    engines_set: str,
    use_google: str,
    use_bing: str,
    use_yandex: str,
    use_indexnow: str,
) -> Optional[dict]:
    if engines_set != "1":
        return None
    return {
        "use_google": 1 if use_google == "1" else 0,
        "use_bing": 1 if use_bing == "1" else 0,
        "use_yandex": 1 if use_yandex == "1" else 0,
        "use_indexnow": 1 if use_indexnow == "1" else 0,
    }


def _account_speed(user: dict) -> dict:
    used = allocated_speed(user["id"])
    projects = active_count(user["id"])
    if user.get("unlimited"):
        return {"cap": None, "used": used, "free": None, "projects": projects, "unlimited": True}
    cap = int(user.get("daily_speed_cap") or 0)
    return {"cap": cap, "used": used, "free": max(0, cap - used), "projects": projects, "unlimited": False}


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    report = bootstrap_db()
    return {"ok": report["database_error"] == "", "version": __version__, **report}


@app.get("/favicon.ico")
async def favicon():
    path = ROOT_DIR / "static" / "favicon.svg"
    if path.exists():
        return FileResponse(path, media_type="image/svg+xml")
    return PlainTextResponse("", status_code=204)


@app.get("/")
async def landing(request: Request):
    return render(request, "landing.html", active="home")


@app.get("/pricing")
async def pricing(request: Request):
    return render(request, "pricing.html", active="pricing")


def _base(request: Request) -> str:
    configured = public_base(request)
    return configured or str(request.base_url).rstrip("/")


@app.get("/login")
async def login_page(request: Request):
    if _user(request):
        return RedirectResponse(_safe_next(request.query_params.get("next") or "/app"), status_code=303)
    return render(request, "login.html", next=request.query_params.get("next") or "/app")


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/app"),
):
    user = get_user_by_username(username)
    if not user or user.get("disabled") or not verify_password(password, user.get("password_hash") or ""):
        return render(
            request,
            "login.html",
            status_code=400,
            error="Username or password is wrong, or this account is disabled.",
            next=next,
        )
    token = create_session(user["id"])
    return _session_response(RedirectResponse(_safe_next(next), status_code=303), request, token)


@app.get("/signup")
async def signup_page(request: Request):
    if _user(request):
        return RedirectResponse("/app", status_code=303)
    return render(request, "signup.html")


@app.post("/signup")
async def signup_submit(
    request: Request,
    username: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
):
    user, error = create_user(username, password, email)
    if not user:
        return render(request, "signup.html", status_code=400, error=error, email=email, username=username)
    token = create_session(user["id"])
    return _session_response(_notice("/app", "Account created."), request, token)


@app.get("/reset")
async def reset_page(request: Request):
    ready, mail_error = describe_mailer()
    return render(request, "reset.html", mail_ready=ready, mail_error="" if ready else mail_error)


@app.post("/reset")
async def reset_submit(request: Request, email: str = Form("")):
    ok, message = issue_reset(email, _base(request))
    return render(
        request,
        "reset.html",
        status_code=200 if ok else 400,
        error="" if ok else message,
        notice=message if ok else "",
    )


@app.get("/reset/confirm")
async def reset_confirm_page(request: Request, token: str = ""):
    return render(request, "reset_confirm.html", token=token)


@app.post("/reset/confirm")
async def reset_confirm_submit(request: Request, token: str = Form(""), password: str = Form("")):
    user, error = consume_reset_token(token)
    if not user:
        return render(request, "reset_confirm.html", status_code=400, error=error, token=token)
    if len(password or "") < 8:
        return render(request, "reset_confirm.html", status_code=400, error="Password must be at least 8 characters.", token=token)
    with tx() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
    session = create_session(user["id"])
    return _session_response(_notice("/app", "Password updated."), request, session)


@app.get("/app/users")
async def users_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        return render(request, "users.html", user=user, active="users", forbidden=True, people=[])
    people = []
    for account in list_users():
        people.append({**account, "used_today": urls_used_today(account["id"])})
    return render(request, "users.html", user=user, active="users", forbidden=False, people=people)


@app.post("/app/users/{user_id}/password")
async def admin_reset_password(user_id: int, request: Request, password: str = Form("")):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only.")
    error = set_user_password(user_id, password)
    if error:
        return _notice("/app/users", error)
    return _notice("/app/users", "Password updated. It was not emailed.")


@app.post("/app/users/{user_id}/disable")
async def admin_disable_user(user_id: int, request: Request, disabled: str = Form("1")):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only.")
    error = set_user_disabled(user_id, disabled == "1")
    if error:
        return _notice("/app/users", error)
    return _notice("/app/users", "Account disabled." if disabled == "1" else "Account enabled.")


@app.post("/logout")
@app.get("/logout")
async def logout(request: Request):
    delete_session(request.cookies.get("bi_session", ""))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("bi_session", path="/")
    return response


@app.get("/app/assistant")
async def assistant_removed():
    return RedirectResponse("/app", status_code=302)


# ---------------------------------------------------------------------------
# App shell
# ---------------------------------------------------------------------------

@app.get("/app")
async def projects_page(request: Request, type: str = "", q: str = "", days: int = 14):
    user, redirect = _need(request)
    if redirect:
        return redirect
    kind = type if type in PROJECT_TYPES else ""
    projects = list_projects(user["id"], project_type=kind, q=q)
    user = get_user(user["id"]) or user
    return render(
        request,
        "projects.html",
        user=user,
        active="projects",
        projects=projects,
        counts=type_counts(user["id"]),
        selected_type=kind,
        q=q,
        speed=_account_speed(user),
        token_cost=get_setting("tokens_per_url") or "1",
        chart=submission_chart(user["id"], days),
    )


@app.get("/app/archive")
async def archive_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    return render(
        request,
        "projects.html",
        user=user,
        active="archive",
        projects=list_projects(user["id"], archived=True),
        counts=type_counts(user["id"]),
        selected_type="",
        q="",
        speed=_account_speed(user),
        token_cost=get_setting("tokens_per_url") or "1",
        chart=submission_chart(user["id"], 14),
        archived_view=True,
    )


@app.get("/app/new")
async def new_project_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    user = get_user(user["id"]) or user
    free = _account_speed(user)["free"]
    return render(
        request,
        "new_project.html",
        user=user,
        active="projects",
        speed=_account_speed(user),
        default_speed=max(1, min(10, free or 1)),
        token_cost=get_setting("tokens_per_url") or "1",
        engines=engine_choices(None),
    )


@app.post("/app/projects")
async def create_project_route(
    request: Request,
    name: str = Form(""),
    project_type: str = Form("indexing"),
    target_domain: str = Form(""),
    daily_speed: int = Form(10),
    urls: str = Form(""),
    upload: Optional[UploadFile] = File(None),
    notes: str = Form(""),
    tags: str = Form(""),
    sitemap_url: str = Form(""),
    autoprocess: str = Form(""),
    engines_set: str = Form(""),
    use_google: str = Form(""),
    use_bing: str = Form(""),
    use_yandex: str = Form(""),
    use_indexnow: str = Form(""),
):
    user, redirect = _need(request)
    if redirect:
        return redirect
    target = registrable_host(target_domain) or ""
    project, error = create_project(
        user,
        name=name,
        target_domain=target,
        project_type=project_type,
        daily_speed=daily_speed,
        notes=notes,
        tags=tags,
        engines=posted_engines(engines_set, use_google, use_bing, use_yandex, use_indexnow),
    )
    if not project:
        user = get_user(user["id"]) or user
        return render(
            request,
            "new_project.html",
            status_code=400,
            user=user,
            active="projects",
            error=error,
            speed=_account_speed(user),
            default_speed=daily_speed,
            token_cost=get_setting("tokens_per_url") or "1",
            form={"name": name, "project_type": project_type, "urls": urls},
            engines=engine_choices(posted_engines(engines_set, use_google, use_bing, use_yandex, use_indexnow)),
        )
    lines = await _lines_from_upload(upload, urls)
    sitemap_note = ""
    if sitemap_url.strip():
        locs, sitemap_note = await _fetch_sitemap(sitemap_url.strip())
        lines.extend(locs)
    result = add_urls(user, project, lines, "Initial batch") if lines else {"added": 0, "duplicates": 0, "skipped": 0, "not_charged": 0, "limit_message": ""}
    message = f"Project created. Added {result['added']} link(s)."
    if result.get("duplicates"):
        message += f" {result['duplicates']} duplicate(s) ignored."
    if result.get("skipped"):
        message += f" {result['skipped']} line(s) were not URLs."
    if result.get("limit_message"):
        message += " " + result["limit_message"]
    if sitemap_note:
        message += " " + sitemap_note
    if autoprocess == "1" and result.get("added"):
        await run_queue(user_id=user["id"], project_id=project["id"])
        message += " Started processing the first batch."
    target_url = _notice(f"/app/projects/{project['id']}", message)
    if autoprocess == "1":
        loc = target_url.headers["location"]
        joiner = "&" if "?" in loc else "?"
        return RedirectResponse(loc + joiner + "continue=1", status_code=303)
    return target_url


@app.get("/app/projects/{project_id}")
async def project_detail(project_id: int, request: Request, q: str = "", filter: str = "all", page: int = 1, per: str = "all"):
    user, redirect = _need(request)
    if redirect:
        return redirect
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    listing = list_urls(project_id, q=q, filt=filter, page=page, per=per)
    return render(
        request,
        "project_detail.html",
        user=user,
        active="projects" if project["run_status"] != "archived" else "archive",
        project=project,
        listing=listing,
        q=q,
        filter=filter,
        token_cost=get_setting("tokens_per_url") or "1",
        speed=_account_speed(get_user(user["id"]) or user),
        engines=engine_choices(project),
        engine_banner="" if any(_configured_map().values()) else (
            "No search engine is configured. Links can still be checked for HTTP status. "
            "Set INDEXNOW_KEY, or Bing, Yandex, and Google keys, before a submission can be accepted."
        ),
    )


@app.post("/app/projects/{project_id}/speed")
async def update_speed(project_id: int, request: Request, daily_speed: int = Form(...)):
    user, redirect = _need(request)
    if redirect:
        return redirect
    user = get_user(user["id"]) or user
    error = set_speed(user, project_id, daily_speed)
    target = f"/app/projects/{project_id}" if request.query_params.get("from") != "list" else "/app"
    if error:
        return _notice(target, error)
    return _notice(target, "Daily speed saved.")


@app.post("/app/projects/{project_id}/stop")
async def stop_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    set_run_status(user["id"], project_id, "stopped")
    return _notice(f"/app/projects/{project_id}", "Project stopped. The queue will skip it.")


@app.post("/app/projects/{project_id}/resume")
async def resume_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    set_run_status(user["id"], project_id, "running")
    return _notice(f"/app/projects/{project_id}", "Project resumed.")


@app.post("/app/projects/{project_id}/archive")
async def archive_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    set_run_status(user["id"], project_id, "archived")
    return _notice("/app/archive", "Project archived. Its daily speed is free to distribute again.")


@app.post("/app/projects/{project_id}/unarchive")
async def unarchive_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    project = get_project(project_id, user["id"])
    user = get_user(user["id"]) or user
    if not project:
        raise HTTPException(404, "Project not found")
    set_run_status(user["id"], project_id, "stopped")
    return _notice(f"/app/projects/{project_id}", "Project restored in the stopped state. Resume it when you want the queue to run.")


@app.post("/app/projects/{project_id}/delete")
async def remove_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    delete_project(user["id"], project_id)
    return _notice("/app", "Project deleted.")


@app.post("/app/projects/{project_id}/engines")
async def save_project_engines(
    project_id: int,
    request: Request,
    engines_set: str = Form(""),
    use_google: str = Form(""),
    use_bing: str = Form(""),
    use_yandex: str = Form(""),
    use_indexnow: str = Form(""),
):
    user, redirect = _need(request)
    if redirect:
        return redirect
    choice = posted_engines(engines_set or "1", use_google, use_bing, use_yandex, use_indexnow) or {}
    error = set_engines(user["id"], project_id, choice)
    return _notice(f"/app/projects/{project_id}", error or "Search engines saved.")


@app.post("/app/projects/{project_id}/process")
async def process_project(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not get_project(project_id, user["id"]):
        raise HTTPException(404, "Project not found")
    result = await run_queue(user_id=user["id"], project_id=project_id)
    project = get_project(project_id, user["id"]) or {}
    queued = int(project.get("queued") or 0)
    message = f"Processed {result['processed']} this batch. {queued} still queued."
    if result.get("stopped_for_time"):
        message += " Stopped at the time limit; the next batch continues automatically."
    if result["processed"] == 0 and queued == 0:
        message = "Nothing was due. The daily speed may already be used, or the queue is empty."
    elif result["processed"] == 0 and not result.get("stopped_for_time"):
        message = "Nothing was due. The daily speed may already be used, or the queue is empty."
    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
        return {
            "ok": True,
            "processed": result["processed"],
            "queued": queued,
            "done": queued == 0,
            "continue": queued > 0 and (result["processed"] > 0 or bool(result.get("stopped_for_time"))),
            "message": message,
        }
    return _notice(f"/app/projects/{project_id}", message)


@app.post("/app/projects/{project_id}/urls")
async def add_project_urls(
    project_id: int,
    request: Request,
    urls: str = Form(""),
    upload: Optional[UploadFile] = File(None),
    sitemap_url: str = Form(""),
    autoprocess: str = Form(""),
    engines_set: str = Form(""),
    use_google: str = Form(""),
    use_bing: str = Form(""),
    use_yandex: str = Form(""),
    use_indexnow: str = Form(""),
):
    user, redirect = _need(request)
    if redirect:
        return redirect
    choice = posted_engines(engines_set, use_google, use_bing, use_yandex, use_indexnow)
    if choice is not None:
        set_engines(user["id"], project_id, choice)
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    lines = await _lines_from_upload(upload, urls)
    sitemap_note = ""
    if sitemap_url.strip():
        locs, sitemap_note = await _fetch_sitemap(sitemap_url.strip())
        lines.extend(locs)
    result = add_urls(user, project, lines, "Added from project")
    message = f"Added {result['added']} link(s)."
    if result.get("duplicates"):
        message += f" {result['duplicates']} duplicate(s)."
    if result.get("skipped"):
        message += f" {result['skipped']} line(s) were not URLs."
    if result.get("limit_message"):
        message += " " + result["limit_message"]
    if sitemap_note:
        message += " " + sitemap_note
    if autoprocess == "1" and result.get("added"):
        await run_queue(user_id=user["id"], project_id=project_id)
        message += " Started processing the first batch."
    target_url = _notice(f"/app/projects/{project_id}", message)
    if autoprocess == "1":
        loc = target_url.headers["location"]
        joiner = "&" if "?" in loc else "?"
        return RedirectResponse(loc + joiner + "continue=1", status_code=303)
    return target_url


@app.post("/app/projects/{project_id}/bulk")
async def bulk(project_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not get_project(project_id, user["id"]):
        raise HTTPException(404, "Project not found")
    form = await request.form()
    ids = _ids(form)
    action = str(form.get("action") or "")
    message = "No links selected."
    if ids and action == "delete":
        with tx() as conn:
            cur = conn.execute(
                f"DELETE FROM urls WHERE project_id = ? AND id IN ({','.join('?' * len(ids))})",
                [project_id, *ids],
            )
        message = f"Deleted {cur.rowcount} link(s)."
    elif ids and action == "hub_on":
        with tx() as conn:
            conn.execute(
                f"UPDATE urls SET in_hub = 1, updated_at = ? WHERE project_id = ? AND id IN ({','.join('?' * len(ids))})",
                [utcnow(), project_id, *ids],
            )
        message = "Selected links are listed on the discovery hub."
    elif ids and action == "hub_off":
        with tx() as conn:
            conn.execute(
                f"UPDATE urls SET in_hub = 0, updated_at = ? WHERE project_id = ? AND id IN ({','.join('?' * len(ids))})",
                [utcnow(), project_id, *ids],
            )
        message = "Selected links were removed from the discovery hub."
    elif ids and action == "requeue":
        count = requeue(user["id"], project_id, ids)
        message = f"Queued {count} link(s) again. Process due now to run them within today's speed."
    elif action == "resubmit_failed":
        count = resubmit_failed(user["id"], project_id, ids or None)
        message = f"Re-queued {count} failed or rejected link(s). Process due now to send them again."
    elif ids and action == "check":
        count, error = await check_ids(user["id"], project_id, ids)
        message = error or f"Google SERP check updated {count} link(s). Yandex and Bing stay unknown until you mark them."
    elif ids and action.startswith("mark_"):
        parts = action.split("_")
        # mark_google_indexed / mark_google_not_indexed
        if len(parts) >= 3 and parts[1] in {"google", "yandex", "bing"}:
            indexed = parts[2] == "indexed" and "not" not in action
            if "not_indexed" in action:
                indexed = False
            count = mark_engines(user["id"], project_id, ids, parts[1], indexed)
            message = f"Manually marked {count} link(s). This does not query the search engine."
        else:
            message = "Unknown mark action."
    elif not ids:
        message = "Select at least one link."
    else:
        message = "Unknown action."
    return _notice(f"/app/projects/{project_id}", message)


@app.get("/app/projects/{project_id}/urls/{url_id}/history")
async def url_history(project_id: int, url_id: int, request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    history = link_history(user["id"], project_id, url_id)
    if history is None:
        raise HTTPException(404, "Link not found")
    return history


@app.get("/app/projects/{project_id}/export.csv")
async def export_csv(project_id: int, request: Request, q: str = "", filter: str = "all"):
    user, redirect = _need(request)
    if redirect:
        return redirect
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    rows = list_urls(project_id, q=q, filt=filter, per="all")["urls"]
    buf = io.StringIO()
    fields = [
        "id", "url", "queue_status", "google_state", "yandex_state", "bing_state",
        "http_status", "is_live", "submission_status", "index_status", "index_source",
        "index_note", "queue_note", "last_checked_at", "last_submitted_at", "created_at",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fields})
    filename = f"backlink-indexer-{project['name'][:40].replace(' ', '_')}.csv"
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/app/plans")
async def plans_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    user = get_user(user["id"]) or user
    return render(request, "plans.html", user=user, active="plans", speed=_account_speed(user))


@app.post("/app/plans/topup")
async def topup(request: Request, amount: int = Form(...)):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        return _notice("/app/plans", "Only the operator can add tokens. Card payments are not connected.")
    if amount < 1 or amount > 1_000_000:
        return _notice("/app/plans", "Enter a token amount between 1 and 1000000.")
    add_tokens(user["id"], amount)
    return _notice("/app/plans", f"Added {amount} tokens to this account.")


@app.get("/app/tools")
async def tools_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    return render(request, "tools.html", user=user, active="tools", hub=hub_urls(request))


@app.get("/app/notifications")
async def notifications_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    rows = get_conn().execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100",
        (user["id"],),
    ).fetchall()
    with tx() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (utcnow(), user["id"]),
        )
    return render(request, "notifications.html", user=user, active="notifications", items=rows)


@app.get("/app/tickets")
async def tickets_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    rows = get_conn().execute(
        "SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC",
        (user["id"],),
    ).fetchall()
    return render(request, "tickets.html", user=user, active="tickets", tickets=rows)


@app.post("/app/tickets")
async def create_ticket(request: Request, subject: str = Form(""), body: str = Form("")):
    user, redirect = _need(request)
    if redirect:
        return redirect
    subject = subject.strip()
    body = body.strip()
    if len(subject) < 3 or len(body) < 3:
        return _notice("/app/tickets", "Add a subject and a message.")
    with tx() as conn:
        conn.execute(
            "INSERT INTO tickets (user_id, subject, body, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (user["id"], subject[:120], body[:4000], utcnow()),
        )
    return _notice("/app/tickets", "Ticket saved.")


@app.get("/app/api")
async def api_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    return render(
        request,
        "api.html",
        user=user,
        active="api",
        key_prefix=api_key_prefix(user["id"]),
        base=str(request.base_url).rstrip("/"),
    )


@app.post("/app/api/rotate")
async def api_rotate(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    raw = rotate_api_key(user["id"])
    return render(
        request,
        "api.html",
        user=user,
        active="api",
        key_prefix=raw[:7],
        new_key=raw,
        base=str(request.base_url).rstrip("/"),
    )


@app.post("/app/api/revoke")
async def api_revoke(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    revoke_api_key(user["id"])
    return _notice("/app/api", "API key revoked.")


@app.get("/app/affiliate")
async def affiliate_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    return render(request, "affiliate.html", user=user, active="affiliate")


@app.get("/app/settings")
async def settings_page(request: Request):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        return render(request, "settings.html", user=user, active="settings", forbidden=True)
    settings = get_settings()
    if settings.get("serp_api_key"):
        settings["serp_api_key"] = _mask(settings["serp_api_key"])
    for setting_key, env_name, flag in (
        ("bing_webmaster_api_key", "BING_WEBMASTER_API_KEY", "bing_from_env"),
        ("yandex_webmaster_token", "YANDEX_WEBMASTER_TOKEN", "yandex_token_from_env"),
    ):
        value, source = setting_or_env(setting_key, env_name)
        settings[flag] = source == "env"
        settings[setting_key] = _mask(value) if value and source == "settings" else ""
    for setting_key, env_name in (
        ("yandex_webmaster_user_id", "YANDEX_WEBMASTER_USER_ID"),
        ("yandex_webmaster_host_id", "YANDEX_WEBMASTER_HOST_ID"),
        ("yandex_site_host", "YANDEX_WEBMASTER_HOST"),
        ("bing_site_url", "BING_WEBMASTER_SITE_URL"),
    ):
        value, source = setting_or_env(setting_key, env_name)
        if source == "env" and not settings.get(setting_key):
            settings[setting_key] = value
    hosts = get_conn().execute("SELECT * FROM owned_hosts ORDER BY host").fetchall()
    info = google_indexing.service_account_info() or {}
    return render(
        request,
        "settings.html",
        user=user,
        active="settings",
        settings=settings,
        hosts=hosts,
        sa_email=info.get("client_email", ""),
        hub=hub_urls(request),
        forbidden=False,
    )


@app.post("/app/settings")
async def save_settings(
    request: Request,
    action: str = Form("save"),
    public_base_url: str = Form(""),
    websub_hub: str = Form(""),
    hub_include: str = Form("all"),
    indexnow_key: str = Form(""),
    serp_provider: str = Form("serpapi"),
    serp_api_key: str = Form(""),
    serp_endpoint: str = Form(""),
    tokens_per_url: str = Form("1"),
    host: str = Form(""),
    host_id: int = Form(0),
    bing_webmaster_api_key: str = Form(""),
    bing_site_url: str = Form(""),
    yandex_webmaster_token: str = Form(""),
    yandex_webmaster_user_id: str = Form(""),
    yandex_webmaster_host_id: str = Form(""),
    yandex_site_host: str = Form(""),
):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        return _notice("/app/settings", "Settings are limited to the operator account.")
    if action == "gen-indexnow":
        set_setting("indexnow_key", indexnow.generate_key())
        return _notice("/app/settings", "New IndexNow key saved.")
    if action == "add-host":
        cleaned = registrable_host(host)
        if not cleaned:
            return _notice("/app/settings", "Enter a hostname such as example.com.")
        with tx() as conn:
            conn.execute(
                "INSERT INTO owned_hosts (host, notes, created_at) VALUES (?, '', ?) ON CONFLICT (host) DO NOTHING",
                (cleaned, utcnow()),
            )
        return _notice("/app/settings", f"Added owned host {cleaned}.")
    if action == "delete-host" and host_id:
        with tx() as conn:
            conn.execute("DELETE FROM owned_hosts WHERE id = ?", (host_id,))
        return _notice("/app/settings", "Owned host removed.")
    if action == "delete-sa":
        set_setting("google_sa_json", "")
        if SA_PATH.exists():
            try:
                SA_PATH.unlink()
            except OSError:
                pass
        return _notice("/app/settings", "Google service account removed.")
    updates = {
        "public_base_url": public_base_url.strip().rstrip("/"),
        "websub_hub": websub_hub.strip() or websub.DEFAULT_HUB,
        "hub_include": hub_include if hub_include in {"all", "live", "has_link"} else "all",
        "serp_provider": serp_provider if serp_provider in {"serpapi", "valueserp", "searchapi", "custom"} else "serpapi",
        "serp_endpoint": serp_endpoint.strip(),
        "tokens_per_url": str(max(0, int(tokens_per_url or "1"))),
    }
    if indexnow_key.strip():
        key = indexnow_key.strip()
        if len(key) < 8 or len(key) > 128:
            return _notice("/app/settings", "IndexNow key must be 8–128 characters.")
        updates["indexnow_key"] = key
    if serp_api_key.strip() and "••••" not in serp_api_key:
        updates["serp_api_key"] = serp_api_key.strip()
    if "••••" not in bing_webmaster_api_key:
        updates["bing_webmaster_api_key"] = bing_webmaster_api_key.strip()
    if "••••" not in yandex_webmaster_token:
        updates["yandex_webmaster_token"] = yandex_webmaster_token.strip()
    updates["bing_site_url"] = bing_site_url.strip()
    updates["yandex_webmaster_user_id"] = yandex_webmaster_user_id.strip()
    updates["yandex_webmaster_host_id"] = yandex_webmaster_host_id.strip()
    updates["yandex_site_host"] = registrable_host(yandex_site_host)
    set_settings(updates)
    return _notice("/app/settings", "Settings saved.")


@app.post("/app/settings/google-sa")
async def upload_sa(request: Request, file: UploadFile = File(...)):
    user, redirect = _need(request)
    if redirect:
        return redirect
    if not user.get("is_admin"):
        return _notice("/app/settings", "Settings are limited to the operator account.")
    raw = await file.read()
    try:
        info = json.loads(raw.decode("utf-8"))
    except Exception:
        return _notice("/app/settings", "That file is not valid JSON.")
    if info.get("type") != "service_account" or "private_key" not in info or "client_email" not in info:
        return _notice("/app/settings", "Upload a Google service-account JSON key.")
    set_setting("google_sa_json", json.dumps(info))
    try:
        SA_PATH.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass
    return _notice("/app/settings", f"Service account saved for {info.get('client_email')}.")


# ---------------------------------------------------------------------------
# API v1
# ---------------------------------------------------------------------------

def _bearer_user(request: Request):
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    return user_from_api_key(header.split(" ", 1)[1])


@app.get("/api/meta")
async def api_meta(request: Request):
    report = bootstrap_db()
    return {"version": __version__, "hub": hub_urls(request), **report}


@app.get("/api/v1/credits")
async def v1_credits(request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    if user.get("unlimited"):
        return {
            "unlimited": True,
            "plan": "VIP",
            "token_balance": None,
            "daily_speed_cap": None,
            "note": "No in-app URL, speed, or token limit. Engine quotas still apply.",
        }
    return {
        "unlimited": False,
        "token_balance": user["token_balance"],
        "daily_speed_cap": user["daily_speed_cap"],
        "plan": user["plan_name"],
    }


@app.get("/api/v1/campaigns")
async def v1_campaigns(request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    return {"projects": list_projects(user["id"])}


@app.post("/api/v1/campaigns")
async def v1_create(request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    payload = await request.json()
    target = registrable_host(str(payload.get("target_domain") or "")) or ""
    project, error = create_project(
        user,
        name=str(payload.get("name") or ""),
        target_domain=target,
        project_type=str(payload.get("project_type") or "indexing"),
        daily_speed=int(payload.get("daily_speed") or 10),
    )
    if not project:
        raise HTTPException(400, error)
    lines = payload.get("urls") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    result = add_urls(user, project, [str(item) for item in lines], "API batch") if lines else {"added": 0}
    return {"project": get_project(project["id"], user["id"]), "ingest": result}


@app.get("/api/v1/campaigns/{project_id}")
async def v1_get(project_id: int, request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    return {"project": project}


@app.get("/api/v1/campaigns/{project_id}/report")
async def v1_report(project_id: int, request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    return {
        "project_id": project_id,
        "total": project["total"],
        "sent": project["sent"],
        "queued": project["queued"],
        "google_indexed": project["google_indexed"],
        "yandex_indexed": project["yandex_indexed"],
        "bing_indexed": project["bing_indexed"],
        "note": "Indexed counts include only SERP checks and manual marks. A submission is not an indexed page.",
    }


@app.post("/api/v1/campaigns/{project_id}/urls")
async def v1_urls(project_id: int, request: Request):
    user = _bearer_user(request)
    if not user:
        raise HTTPException(401, "Send Authorization: Bearer <api key>.")
    project = get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404, "Project not found")
    payload = await request.json()
    lines = payload.get("urls") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    return add_urls(user, project, [str(item) for item in lines], "API batch")


@app.api_route("/api/cron/tick", methods=["GET", "POST"])
async def cron_tick(request: Request):
    if not _cron_ok(request):
        raise HTTPException(401, "Cron authentication failed.")
    result = await run_queue()
    report = bootstrap_db()
    return {"ok": True, **{k: v for k, v in result.items() if k != "ping"}, "persistent_db": report["persistent_db"]}


# ---------------------------------------------------------------------------
# Discovery hub (public)
# ---------------------------------------------------------------------------

@app.get("/robots.txt")
async def robots_txt(request: Request):
    body = (
        "User-agent: *\nAllow: /hub\nAllow: /hub/\nAllow: /robots.txt\n"
        "Disallow: /api/\nDisallow: /app\nDisallow: /static/\n"
        f"Sitemap: {hub_urls(request)['sitemap']}\n"
    )
    return PlainTextResponse(body)


@app.get("/hub")
async def hub_html(request: Request):
    rows = _hub_rows()
    return render(
        request,
        "hub.html",
        urls=rows,
        hub=hub_urls(request),
        generated=utcnow(),
        count=len(rows),
    )


@app.get("/hub/sitemap.xml")
async def hub_sitemap(request: Request):
    rows = _hub_rows()
    today = utcnow()[:10]
    hub = hub_urls(request)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "  <url>",
        f"    <loc>{xml_escape(hub['hub'])}</loc>",
        f"    <lastmod>{today}</lastmod>",
        "  </url>",
    ]
    for row in rows:
        lastmod = (row.get("updated_at") or row.get("created_at") or today)[:10]
        parts.append("  <url>")
        parts.append(f"    <loc>{xml_escape(row['url'])}</loc>")
        parts.append(f"    <lastmod>{xml_escape(lastmod)}</lastmod>")
        parts.append("  </url>")
    parts.append("</urlset>")
    return Response("\n".join(parts) + "\n", media_type="application/xml")


@app.get("/hub/feed.xml")
async def hub_rss(request: Request):
    rows = _hub_rows()
    hub = hub_urls(request)
    items = []
    for row in rows[:200]:
        items.append(
            "<item>"
            f"<title>{xml_escape(row['url'])}</title>"
            f"<link>{xml_escape(row['url'])}</link>"
            f"<guid isPermaLink=\"true\">{xml_escape(row['url'])}</guid>"
            "<description>Submitted URL listed for crawl discovery.</description>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Backlink Indexer discovery feed</title>"
        f"<link>{xml_escape(hub['hub'])}</link>"
        "<description>Submitted URLs listed for crawl discovery. Listing is not indexing.</description>"
        + "".join(items)
        + "</channel></rss>"
    )
    return Response(xml, media_type="application/rss+xml")


@app.get("/hub/atom.xml")
async def hub_atom(request: Request):
    rows = _hub_rows()
    hub = hub_urls(request)
    now = utcnow()
    items = []
    for row in rows[:200]:
        items.append(
            "<entry>"
            f"<title>{xml_escape(row['url'])}</title>"
            f'<link href="{xml_escape(row["url"])}"/>'
            f"<id>{xml_escape(row['url'])}</id>"
            f"<updated>{xml_escape(row.get('updated_at') or now)}</updated>"
            "<summary>Submitted URL listed for crawl discovery.</summary>"
            "</entry>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Backlink Indexer discovery feed</title>"
        f'<link href="{xml_escape(hub["atom"])}" rel="self"/>'
        f"<updated>{now}</updated>"
        f"<id>{xml_escape(hub['atom'])}</id>"
        + "".join(items)
        + "</feed>"
    )
    return Response(xml, media_type="application/atom+xml")


@app.get("/{key}.txt")
async def indexnow_key_file(key: str):
    configured = setting_or_env("indexnow_key", "INDEXNOW_KEY")[0]
    if not configured or key != configured:
        raise HTTPException(404, "Unknown IndexNow key")
    return PlainTextResponse(configured)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "••••••••"
    return value[:3] + "••••" + value[-2:]


if __name__ == "__main__":
    import uvicorn
    from .config import HOST, PORT

    uvicorn.run("backlink_indexer.main:app", host=HOST, port=PORT, reload=False)
