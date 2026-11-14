"""Per-link submission history and the projects chart."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .database import get_conn

ENGINES = (
    ("google", "Google", "#7b2ff7"),
    ("bing", "Bing", "#ff1aa8"),
    ("yandex", "Yandex", "#6a2ee8"),
    ("indexnow", "IndexNow", "#ff6eb4"),
)


def link_history(user_id: int, project_id: int, url_id: int) -> Optional[dict]:
    row = get_conn().execute(
        """SELECT u.url FROM urls u
           JOIN projects p ON p.id = u.project_id
           WHERE u.id = ? AND u.project_id = ? AND p.user_id = ?""",
        (url_id, project_id, user_id),
    ).fetchone()
    if not row:
        return None
    logs = get_conn().execute(
        """SELECT method, status, detail, created_at
           FROM submission_log WHERE url_id = ?
           ORDER BY id DESC LIMIT 100""",
        (url_id,),
    ).fetchall()
    return {"url": row["url"], "attempts": [dict(item) for item in logs]}


def submission_chart(user_id: int, days: int = 14) -> dict:
    window = 30 if int(days or 14) == 30 else 14
    today = datetime.now(timezone.utc).date()
    dates = [today - timedelta(days=window - 1 - index) for index in range(window)]
    start = dates[0].isoformat()
    counts = {key: {day.isoformat(): 0 for day in dates} for key, _label, _color in ENGINES}
    rows = get_conn().execute(
        """SELECT substr(l.created_at, 1, 10) AS day, l.method AS method, COUNT(*) AS c
           FROM submission_log l
           JOIN urls u ON u.id = l.url_id
           JOIN projects p ON p.id = u.project_id
           WHERE p.user_id = ? AND substr(l.created_at, 1, 10) >= ?
           GROUP BY day, method""",
        (user_id, start),
    ).fetchall()
    for row in rows:
        bucket = counts.get(row["method"])
        if bucket is not None and row["day"] in bucket:
            bucket[row["day"]] = int(row["c"])
    peak = max((value for bucket in counts.values() for value in bucket.values()), default=0)
    peak = max(peak, 1)
    width = 640
    height = 210
    left, right, top, baseline = 36, 16, 18, 168
    plot = width - left - right
    step = plot / (window - 1) if window > 1 else plot
    series = []
    for key, label, color in ENGINES:
        points = []
        for index, day in enumerate(dates):
            value = counts[key][day.isoformat()]
            x = left + index * step
            y = baseline - (value / peak) * (baseline - top)
            points.append(f"{x:.1f},{y:.1f}")
        series.append({
            "key": key,
            "label": label,
            "color": color,
            "total": sum(counts[key].values()),
            "points": " ".join(points),
        })
    label_every = 7 if window > 14 else 2
    labels = []
    for index, day in enumerate(dates):
        if index % label_every == 0 or index == window - 1:
            labels.append({"x": round(left + index * step, 1), "text": day.strftime("%m-%d")})
    return {
        "days": window,
        "peak": peak,
        "width": width,
        "height": height,
        "baseline": baseline,
        "series": series,
        "labels": labels,
        "empty": peak == 1 and all(item["total"] == 0 for item in series),
    }
