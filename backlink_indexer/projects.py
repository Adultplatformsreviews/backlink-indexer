from __future__ import annotations

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
        where.append("(p.name LIKE ? OR p.target_domain LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like])
    rows = get_conn().execute(
        f"""SELECT p.*, {_STATS}
            FROM projects p
            WHERE {' AND '.join(where)}
            ORDER BY p.id DESC""",
        params,
    ).fetchall()
    return [_decorate(row) for row in rows]


def create_project(
    user: dict,
    *,
    name: str,
    target_domain: str,
    project_type: str,
    daily_speed: int,
    notes: str = "",
    tags: str = "",
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
    now = utcnow()
    with tx() as conn:
        project_id = insert_id(
            conn,
            """INSERT INTO projects
               (user_id, name, target_domain, include_subdomains, project_type,
                daily_speed, run_status, sent_today, sent_on, created_at, updated_at,
                notes, tags)
               VALUES (?, ?, ?, 1, ?, ?, 'running', 0, '', ?, ?, ?, ?)""",
            (user["id"], name, target_domain, project_type, daily_speed, now, now, notes, tags),
        )
    return get_project(project_id, user["id"]), ""


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


def list_urls(
    project_id: int,
    *,
    q: str = "",
    filt: str = "all",
    page: int = 1,
    per: int = 10,
) -> dict:
    if per not in {5, 10, 25}:
        per = 10
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
        "indexed": "(google_state = 'indexed' OR yandex_state = 'indexed' OR bing_state = 'indexed')",
        "unknown": "google_state = 'unknown' AND yandex_state = 'unknown' AND bing_state = 'unknown'",
        "dead": "is_live = 0",
    }
    if filt in filters:
        where.append(filters[filt])
    where_sql = " AND ".join(where)
    total = int(
        get_conn().execute(f"SELECT COUNT(*) AS c FROM urls WHERE {where_sql}", params).fetchone()["c"]
    )
    pages = max(1, (total + per - 1) // per)
    if page > pages:
        page = pages
    rows = get_conn().execute(
        f"""SELECT * FROM urls WHERE {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?""",
        [*params, per, (page - 1) * per],
    ).fetchall()
    return {
        "urls": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "per": per,
        "pages": pages,
    }


def delete_project(user_id: int, project_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        )
        return cur.rowcount > 0
