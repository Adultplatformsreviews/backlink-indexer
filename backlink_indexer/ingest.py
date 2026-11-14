from __future__ import annotations

import csv
import io

from .database import insert_id, tx, utcnow
from .limits import record_added, reserve_slots
from .services.urlutil import normalize_submitted


def parse_upload(text: str, filename: str) -> list[str]:
    lines: list[str] = []
    lower = (filename or "").lower()
    if lower.endswith(".xml") or "<loc>" in (text or "").lower():
        locs = sitemap_locs(text)
        if locs:
            return locs
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


def normalize_lines(raw_lines: list[str]) -> tuple[list[str], int, int]:
    seen: set[str] = set()
    urls: list[str] = []
    skipped = 0
    duplicates = 0
    for line in raw_lines:
        piece = line.split(",")[0] if "," in line and not line.strip().lower().startswith("http") else line
        url = normalize_submitted(piece)
        if not url:
            if line.strip():
                skipped += 1
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        urls.append(url)
    return urls, skipped, duplicates


def sitemap_locs(xml_text: str) -> list[str]:
    import re
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text or "", flags=re.I)


def add_urls(user: dict, project: dict, raw_lines: list[str], batch_name: str) -> dict:
    """Add every valid URL. Duplicates are counted and skipped. There is no token charge."""
    urls, skipped, duplicates = normalize_lines(raw_lines)
    if not urls:
        return {
            "added": 0, "duplicates": duplicates, "skipped": skipped, "not_charged": 0,
            "charge": 0, "batch_id": None, "limit_message": "",
        }
    allowed, limit_message = reserve_slots(user, len(urls))
    urls = urls[:allowed]
    if not urls:
        return {
            "added": 0, "duplicates": 0, "skipped": skipped, "not_charged": 0,
            "charge": 0, "batch_id": None, "limit_message": limit_message,
        }
    now = utcnow()
    in_hub = 0 if project.get("project_type") == "check" else 1
    added = 0
    with tx() as conn:
        batch_id = insert_id(
            conn,
            "INSERT INTO batches (project_id, name, created_at) VALUES (?, ?, ?)",
            (project["id"], batch_name or f"Batch {now[:16].replace('T', ' ')} UTC", now),
        )
        for url in urls:
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
    record_added(user["id"], added)
    return {
        "added": added,
        "duplicates": duplicates,
        "skipped": skipped,
        "not_charged": 0,
        "charge": 0,
        "batch_id": batch_id,
        "limit_message": limit_message,
    }
