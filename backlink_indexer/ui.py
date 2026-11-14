from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import user_from_session
from .database import bootstrap_db, get_conn
from .limits import usage_status
from .projects import TYPE_LABELS, archive_count, active_count
from .services.quotas import snapshot as quota_snapshot

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

STATE_LABELS = {
    "unknown": "Unknown",
    "submitted": "Submitted",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "indexed": "Indexed",
    "not_indexed": "Not indexed",
    "skipped": "Skipped",
    "queued": "Queued",
    "sent": "Sent",
    "failed": "Failed",
    "processing": "Processing",
    "running": "In progress",
    "stopped": "Stopped",
    "archived": "Archived",
    "pending": "Pending",
    "submitted_hub": "On hub",
    "checked": "Checked",
    "check_pending": "Not checked",
}


def fmt_date(iso: str) -> str:
    if not iso:
        return "—"
    day = str(iso)[:10]
    parts = day.split("-")
    if len(parts) == 3 and all(parts):
        year, month, day_n = parts
        return f"{day_n}.{month}.{year}"
    return day


def state_label(value: str) -> str:
    return STATE_LABELS.get(value or "", value or "—")


def type_label(value: str) -> str:
    return TYPE_LABELS.get(value or "", value or "")


templates.env.globals["fmt_date"] = fmt_date
templates.env.globals["state_label"] = state_label
templates.env.globals["type_label"] = type_label
templates.env.globals["version"] = __version__


def current_user(request: Request):
    token = request.cookies.get("bi_session", "")
    return user_from_session(token)


def nav_counts(user_id: int) -> dict:
    unread = get_conn().execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()
    return {
        "projects_count": active_count(user_id),
        "archive_count": archive_count(user_id),
        "unread": int(unread["c"] or 0) if unread else 0,
    }


def render(request: Request, name: str, status_code: int = 200, **ctx):
    user = ctx.pop("user", None)
    if user is None:
        user = current_user(request)
    base = {
        "request": request,
        "user": user,
        "version": __version__,
        "notice": request.query_params.get("notice", ""),
        "active": ctx.get("active", ""),
        "projects_count": 0,
        "archive_count": 0,
        "unread": 0,
        "usage": None,
        "engine_quotas": [],
    }
    if user:
        base.update(nav_counts(user["id"]))
        base["usage"] = usage_status(user)
        base["engine_quotas"] = quota_snapshot()
    report = bootstrap_db()
    base["ephemeral"] = report["ephemeral_storage"]
    base["database_env"] = report["database_env"]
    base["database_error"] = report["database_error"]
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(nxt, safe='/?=&')}", status_code=303)


def require_user(request: Request):
    user = current_user(request)
    if not user:
        return None
    return user
