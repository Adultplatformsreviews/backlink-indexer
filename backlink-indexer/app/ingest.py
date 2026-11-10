from __future__ import annotations

import csv
import io

from .database import get_conn, get_setting, insert_id, tx, utcnow
from .services.urlutil import normalize_submitted


def parse_upload(text: str, filename: str) -> list[str]:
    lines: list[str] = []
    lower = (filename or "").lower()
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
            if header and not normalize_submitted(rows[0][0] if rows[0] else ""):
                rows = rows[1:]
        for row in rows:
            if row and url_idx < len(row):
                lines.append(row[url_idx])
        return lines
    return text.splitlines()


def normalize_lines(raw_lines: list[str]) -> tuple[list[str], int]:
    seen: set[str] = set()
    urls: list[str] = []
    skipped = 0
    for line in raw_lines:
        piece = line.split(",")[0] if "," in line and not line.strip().lower().startswith("http") else line
        url = normalize_submitted(piece)
        if not url:
            if line.strip():
                skipped += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls, skipped


def add_urls(user: dict, project: dict, raw_lines: list[str], batch_name: str) -> dict:
    urls, skipped = normalize_lines(raw_lines)
    try:
        cost = max(0, int(get_setting("tokens_per_url") or "1"))
    except ValueError:
        cost = 1
    if not urls:
        balance = int(get_conn().execute(
            "SELECT token_balance FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["token_balance"])
        return {
            "added": 0,
            "duplicates": 0,
            "skipped": skipped,
            "not_charged": 0,
            "charge": 0,
            "balance": balance,
            "batch_id": None,
        }
    now = utcnow()
    in_hub = 0 if project.get("project_type") == "check" else 1
    with tx() as conn:
        balance = int(
            conn.execute("SELECT token_balance FROM users WHERE id = ?", (user["id"],)).fetchone()[
                "token_balance"
            ]
        )
        affordable = len(urls) if cost == 0 else balance // cost
        take = urls[:affordable]
        not_charged = len(urls) - len(take)
        added = 0
        duplicates = 0
        batch_id = None
        if take:
            batch_id = insert_id(
                conn,
                "INSERT INTO batches (project_id, name, created_at) VALUES (?, ?, ?)",
                (project["id"], batch_name or f"Batch {now[:16].replace('T', ' ')} UTC", now),
            )
        for url in take:
            try:
                conn.execute(
                    """INSERT INTO urls
                       (project_id, batch_id, url, in_hub, submission_status, index_status,
                        google_state, yandex_state, bing_state, queue_status, queue_note,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'pending', 'unknown',
                               'unknown', 'unknown', 'unknown', 'queued', '', ?, ?)""",
                    (project["id"], batch_id, url, in_hub, now, now),
                )
                added += 1
            except Exception:
                duplicates += 1
        charge = added * cost
        if charge:
            cur = conn.execute(
                """UPDATE users SET token_balance = token_balance - ?
                   WHERE id = ? AND token_balance >= ?""",
                (charge, user["id"], charge),
            )
            if cur.rowcount == 0:
                raise RuntimeError("Token balance changed before the URLs could be saved.")
        balance = int(
            conn.execute("SELECT token_balance FROM users WHERE id = ?", (user["id"],)).fetchone()[
                "token_balance"
            ]
        )
    return {
        "added": added,
        "duplicates": duplicates,
        "skipped": skipped,
        "not_charged": not_charged,
        "charge": charge,
        "balance": balance,
        "batch_id": batch_id,
    }
