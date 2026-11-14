from __future__ import annotations

import re
from typing import Optional

from .database import get_conn, insert_id, tx, utcnow

PROJECT_TYPES = ("indexing", "instant", "check")
TYPE_LABELS = {
    "indexing": "Indexing",
    "instant": "Instant indexing",
    "check": "Indexing check",
}
RUN_LABELS = {
    "running": "In progress",
    "stopped": "Stopped",
    "archived": "Archived",
}


def _decorate(row: dict) -> dict:
    data = dict(row)
    total = int(data.get("total") or 0)
    handled = int(data.get("handled") or 0)
    data["total"] = total
    data["queued"] = int(data.get("queued") or 0)
    data["sent"] = int(data.get("sent") or 0)
    data["handled"] = handled
    data["google_indexed"] = int(data.get("google_indexed") or 0)
    data["yandex_indexed"] = int(data.get("yandex_indexed") or 0)
    data["bing_indexed"] = int(data.get("bing_indexed") or 0)
    data["google_accepted"] = int(data.get("google_accepted") or 0)
    data["yandex_accepted"] = int(data.get("yandex_accepted") or 0)
    data["bing_accepted"] = int(data.get("bing_accepted") or 0)
    data["progress"] = round(100 * handled / total) if total else 0
    data["type_label"] = TYPE_LABELS.get(data.get("project_type") or "", data.get("project_type") or "")
    data["run_label"] = RUN_LABELS.get(data.get("run_status") or "", data.get("run_status") or "")
    return data


_STATS = """
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id) AS total,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.queue_status = 'queued') AS queued,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.queue_status = 'sent') AS sent,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.queue_status IN ('sent', 'skipped', 'failed')) AS handled,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.google_state = 'indexed') AS google_indexed,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.yandex_state = 'indexed') AS yandex_indexed,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.bing_state = 'indexed') AS bing_indexed,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.google_state IN ('accepted', 'submitted')) AS google_accepted,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.yandex_state IN ('accepted', 'submitted')) AS yandex_accepted,
    (SELECT COUNT(*) FROM urls u WHERE u.project_id = p.id AND u.bing_state IN ('accepted', 'submitted')) AS bing_accepted
"""


def get_project(project_id: int, user_id: int) -> Optional[dict]:
    row = get_conn().execute(
        f"SELECT p.*, {_STATS} FROM projects p WHERE p.id = ? AND p.user_id = ?",
        (project_id, user_id),
    ).fetchone()
    return _decorate(row) if row else None


def allocated_speed(user_id: int, exclude_project: Optional[int] = None) -> int:
    sql = """SELECT COALESCE(SUM(daily_speed), 0) AS s FROM projects
             WHERE user_id = ? AND run_status != 'archived'"""
    params: list = [user_id]
    if exclude_project:
        sql += " AND id != ?"
        params.append(exclude_project)
    row = get_conn().execute(sql, params).fetchone()
    return int(row["s"] or 0)


def active_count(user_id: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM projects WHERE user_id = ? AND run_status != 'archived'",
        (user_id,),
    ).fetchone()
    return int(row["c"] or 0)


def archive_count(user_id: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS c FROM projects WHERE user_id = ? AND run_status = 'archived'",
        (user_id,),
    ).fetchone()
    return int(row["c"] or 0)


def type_counts(user_id: int) -> dict[str, int]:
    rows = get_conn().execute(
        """SELECT project_type, COUNT(*) AS c FROM projects
           WHERE user_id = ? AND run_status != 'archived'
           GROUP BY project_type""",
        (user_id,),
    ).fetchall()
    counts = {key: 0 for key in PROJECT_TYPES}
    for row in rows:
        counts[row["project_type"]] = int(row["c"] or 0)
    counts["all"] = sum(counts.values())
    return counts


def list_projects(
    user_id: int,
    *,
    archived: bool = False,
    project_type: str = "",
    q: str = "",
) -> list[dict]:
    where = ["p.user_id = ?"]
    params: list = [user_id]
    if archived:
        where.append("p.run_status = 'archived'")
    else:
        where.append("p.run_status != 'archived'")
    if project_type in PROJECT_TYPES:
        where.append("p.project_type = ?")
        params.append(project_type)
    if q.strip():
        where.append("p.name LIKE ?")
        params.append(f"%{q.strip()}%")
    rows = get_conn().execute(
        f"""SELECT p.*, {_STATS}
            FROM projects p
            WHERE {' AND '.join(where)}
            ORDER BY p.id DESC""",
        params,
    ).fetchall()
    return [_decorate(row) for row in rows]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]
    return slug or "project"


def unique_slug(name: str, project_id: Optional[int] = None) -> str:
    base = slugify(name)
    slug = base
    number = 2
    while True:
        row = get_conn().execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if not row or (project_id is not None and int(row["id"]) == int(project_id)):
            return slug
        slug = f"{base}-{number}"[:60]
        number += 1


def backfill_crawl_hub() -> int:
    """Give every pre-existing project a slug and turn the crawl hub on.

    A project that already has a slug and publish_hub 0 was switched off on
    purpose and is left alone. An empty slug means the hub feature never ran
    for that row, so it is published by default.
    """
    rows = get_conn().execute(
        "SELECT id, name, slug, publish_hub FROM projects ORDER BY id"
    ).fetchall()
    changed = 0
    for row in rows:
        slug = (row["slug"] or "").strip()
        publish = row["publish_hub"]
        new_slug = slug
        new_publish = publish
        if not slug:
            new_slug = unique_slug(row["name"] or "project", int(row["id"]))
            new_publish = 1
        elif publish is None:
            new_publish = 1
        else:
            continue
        with tx() as conn:
            conn.execute(
                "UPDATE projects SET slug = ?, publish_hub = ? WHERE id = ?",
                (new_slug, int(new_publish or 0), row["id"]),
            )
        changed += 1
    return changed


def ensure_slug(project: dict) -> dict:
    if project.get("slug"):
        return project
    slug = unique_slug(project.get("name") or "project", project["id"])
    with tx() as conn:
        conn.execute("UPDATE projects SET slug = ? WHERE id = ?", (slug, project["id"]))
    project["slug"] = slug
    return project


def create_project(
    user: dict,
    *,
    name: str,
    target_domain: str = "",
    project_type: str,
    daily_speed: int,
    notes: str = "",
    tags: str = "",
    engines: Optional[dict] = None,
) -> tuple[Optional[dict], str]:
    name = (name or "").strip()
    if not name or len(name) > 80:
        return None, "Enter a project name (80 characters or fewer)."
    if project_type not in PROJECT_TYPES:
        return None, "Choose Indexing, Instant indexing, or Indexing check."
    try:
        daily_speed = int(daily_speed)
    except (TypeError, ValueError):
        return None, "Daily speed must be a number."
    if daily_speed < 1:
        return None, "Daily speed must be at least 1."
    notes = (notes or "").strip()[:500]
    tags = (tags or "").strip()[:200]
    target_domain = (target_domain or "").strip()
    flags = engines or {}
    now = utcnow()
    slug = unique_slug(name)
    with tx() as conn:
        project_id = insert_id(
            conn,
            """INSERT INTO projects
               (user_id, name, target_domain, include_subdomains, project_type,
                daily_speed, run_status, sent_today, sent_on, created_at, updated_at,
                notes, tags, use_google, use_bing, use_yandex, use_indexnow, slug, publish_hub)
               VALUES (?, ?, ?, 1, ?, ?, 'running', 0, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                user["id"], name, target_domain, project_type, daily_speed, now, now, notes, tags,
                int(flags.get("use_google", 1)),
                int(flags.get("use_bing", 1)),
                int(flags.get("use_yandex", 1)),
                int(flags.get("use_indexnow", 1)),
                slug,
            ),
        )
    return get_project(project_id, user["id"]), ""


def set_publish_hub(user_id: int, project_id: int, publish: bool) -> str:
    project = get_project(project_id, user_id)
    if not project:
        return "Project not found."
    with tx() as conn:
        conn.execute(
            "UPDATE projects SET publish_hub = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (1 if publish else 0, utcnow(), project_id, user_id),
        )
    return ""


def set_engines(user_id: int, project_id: int, engines: dict) -> str:
    project = get_project(project_id, user_id)
    if not project:
        return "Project not found."
    with tx() as conn:
        conn.execute(
            """UPDATE projects
               SET use_google=?, use_bing=?, use_yandex=?, use_indexnow=?, updated_at=?
               WHERE id=? AND user_id=?""",
            (
                int(engines.get("use_google", 0)),
                int(engines.get("use_bing", 0)),
                int(engines.get("use_yandex", 0)),
                int(engines.get("use_indexnow", 0)),
                utcnow(),
                project_id,
                user_id,
            ),
        )
    return ""


def set_speed(user: dict, project_id: int, daily_speed: int) -> str:
    project = get_project(project_id, user["id"])
    if not project:
        return "Project not found."
    if project["run_status"] == "archived":
        return "Unarchive the project before changing its speed."
    try:
        daily_speed = int(daily_speed)
    except (TypeError, ValueError):
        return "Daily speed must be a number."
    if daily_speed < 1:
        return "Daily speed must be at least 1."
    with tx() as conn:
        conn.execute(
            "UPDATE projects SET daily_speed = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (daily_speed, utcnow(), project_id, user["id"]),
        )
    return ""


def set_run_status(user_id: int, project_id: int, status: str) -> bool:
    if status not in {"running", "stopped", "archived"}:
        return False
    with tx() as conn:
        cur = conn.execute(
            "UPDATE projects SET run_status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (status, utcnow(), project_id, user_id),
        )
        return cur.rowcount > 0


PAGE_SIZES = (10, 50, 100)


def list_urls(
    project_id: int,
    *,
    q: str = "",
    filt: str = "all",
    page: int = 1,
    per: int | str = 0,
) -> dict:
    """Return matching links. per=0 or 'all' returns every row; 10, 50, and 100 paginate."""
    token = str(per).strip().lower() if per is not None else "all"
    if token in {"", "all", "0"}:
        page_size = 0
    else:
        try:
            requested = int(token)
        except (TypeError, ValueError):
            requested = 0
        page_size = requested if requested in PAGE_SIZES else 0
    page = max(1, int(page or 1))
    where = ["project_id = ?"]
    params: list = [project_id]
    if q.strip():
        where.append("url LIKE ?")
        params.append(f"%{q.strip()}%")
    filters = {
        "queued": "queue_status = 'queued'",
        "sent": "queue_status = 'sent'",
        "skipped": "queue_status = 'skipped'",
        "failed": "queue_status = 'failed'",
        "rejected": "(google_state = 'rejected' OR yandex_state = 'rejected' OR bing_state = 'rejected')",
        "indexed": "(google_state = 'indexed' OR yandex_state = 'indexed' OR bing_state = 'indexed')",
        "unknown": "google_state = 'unknown' AND yandex_state = 'unknown' AND bing_state = 'unknown'",
        "dead": "is_live = 0",
        "internal": "link_type = 'internal'",
        "external": "link_type = 'external'",
    }
    if filt in filters:
        where.append(filters[filt])
    where_sql = " AND ".join(where)
    total = int(
        get_conn().execute(f"SELECT COUNT(*) AS c FROM urls WHERE {where_sql}", params).fetchone()["c"]
    )
    if page_size == 0:
        rows = get_conn().execute(
            f"SELECT * FROM urls WHERE {where_sql} ORDER BY id DESC",
            params,
        ).fetchall()
        return {
            "urls": [dict(row) for row in rows],
            "total": total,
            "page": 1,
            "per": 0,
            "pages": 1,
        }
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    rows = get_conn().execute(
        f"""SELECT * FROM urls WHERE {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "urls": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "per": page_size,
        "pages": pages,
    }


def delete_project(user_id: int, project_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        )
        return cur.rowcount > 0
