from __future__ import annotations

import csv
import io

from .config import PUBLIC_BASE_URL
from .credits import deduct, trim_for_balance, unit_cost
from .database import get_conn, get_setting, insert_id, tx, utcnow
from .limits import record_added, reserve_slots
from .services import bing_webmaster, gsc
from .services.urlutil import canonicalize, host_of, normalize_submitted, registrable_host


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


def _owned_hosts() -> set[str]:
    rows = get_conn().execute("SELECT host FROM owned_hosts").fetchall()
    hosts = {registrable_host(row["host"]) for row in rows}
    base = get_setting("public_base_url") or PUBLIC_BASE_URL
    if base:
        hosts.add(registrable_host(host_of(base)))
    return {host for host in hosts if host}


def classify_link(url: str, choice: str, bing_sites: list[str] | None = None) -> str:
    """Resolve Auto-detect. Internal when the host is owned or verified in Bing."""
    picked = (choice or "auto").strip().lower()
    if picked == "internal":
        return "internal"
    if picked == "external":
        return "external"
    owned = _owned_hosts()
    host = registrable_host(host_of(url))
    if any(host == item or host.endswith("." + item) for item in owned):
        return "internal"
    if bing_webmaster.match_site(url, bing_sites or []):
        return "internal"
    return "external"


def prepare_urls(raw_lines: list[str]) -> tuple[list[str], int, list[str]]:
    """Canonicalize and dedupe inside the pasted batch."""
    seen: set[str] = set()
    urls: list[str] = []
    skipped = 0
    skipped_duplicates: list[str] = []
    for line in raw_lines:
        piece = line.split(",")[0] if "," in line and not line.strip().lower().startswith("http") else line
        url = canonicalize(piece)
        if not url:
            if line.strip():
                skipped += 1
            continue
        if url in seen:
            skipped_duplicates.append(url)
            continue
        seen.add(url)
        urls.append(url)
    return urls, skipped, skipped_duplicates


def _empty_ingest(skipped: int, skipped_duplicates: list[str], limit_message: str = "") -> dict:
    return {
        "added": 0,
        "duplicates": len(skipped_duplicates),
        "skipped": skipped,
        "not_charged": 0,
        "charge": 0,
        "batch_id": None,
        "limit_message": limit_message,
        "accepted": [],
        "not_sendable": [],
        "skipped_duplicates": list(skipped_duplicates),
        "ids": [],
    }


def add_urls(
    user: dict,
    project: dict,
    raw_lines: list[str],
    batch_name: str,
    link_type: str = "auto",
    bing_sites: list[str] | None = None,
) -> dict:
    """Add every valid URL. Credits are reserved, not spent, until a URL is confirmed indexed."""
    urls, skipped, skipped_duplicates = prepare_urls(raw_lines)
    if not urls:
        return _empty_ingest(skipped, skipped_duplicates)
    allowed, limit_message = reserve_slots(user, len(urls))
    urls = urls[:allowed]
    if not urls:
        return _empty_ingest(skipped, skipped_duplicates, limit_message)
    now = utcnow()
    in_hub = 0 if project.get("project_type") == "check" else 1
    cost = unit_cost(user)
    added = 0
    accepted: list[dict] = []
    ids: list[int] = []
    with tx() as conn:
        urls, credit_message = trim_for_balance(conn, int(user["id"]), urls, cost)
        if credit_message:
            limit_message = (limit_message + " " + credit_message).strip()
        if not urls:
            return _empty_ingest(skipped, skipped_duplicates, limit_message)
        existing_rows = conn.execute(
            "SELECT url, normalized_url FROM urls WHERE project_id = ?",
            (project["id"],),
        ).fetchall()
        have = set()
        for existing in existing_rows:
            if existing["url"]:
                have.add(existing["url"])
            if existing["normalized_url"]:
                have.add(existing["normalized_url"])
        fresh = []
        for url in urls:
            if url in have:
                skipped_duplicates.append(url)
                continue
            have.add(url)
            fresh.append(url)
        urls = fresh
        if not urls:
            return _empty_ingest(skipped, skipped_duplicates, limit_message)
        batch_id = insert_id(
            conn,
            "INSERT INTO batches (project_id, name, created_at) VALUES (?, ?, ?)",
            (project["id"], batch_name or f"Batch {now[:16].replace('T', ' ')} UTC", now),
        )
        for url in urls:
            try:
                link = classify_link(url, link_type, bing_sites)
                kind = gsc.classify_kind(int(user["id"]), url)
                url_id = insert_id(
                    conn,
                    """INSERT INTO urls
                       (project_id, batch_id, url, normalized_url, host, kind, in_hub, link_type,
                        submission_status, index_status, google_state, yandex_state, bing_state,
                        queue_status, queue_note, status, precheck_json, credits_held, credits_state,
                        next_check_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'unknown',
                               'unknown', 'unknown', 'unknown', 'queued', '', 'queued', '', ?, ?, ?, ?, ?)""",
                    (
                        project["id"], batch_id, url, url, host_of(url), kind, in_hub, link,
                        cost, "reserved" if cost else "", now, now, now,
                    ),
                )
                added += 1
                ids.append(url_id)
                accepted.append({"id": url_id, "url": url, "status": "queued", "kind": kind})
            except Exception:
                skipped_duplicates.append(url)
        if cost and added and not deduct(conn, int(user["id"]), added * cost):
            raise RuntimeError("Credit balance changed while reserving URLs.")
    record_added(user["id"], added)
    return {
        "added": added,
        "duplicates": len(skipped_duplicates),
        "skipped": skipped,
        "not_charged": 0,
        "charge": added * cost,
        "batch_id": batch_id,
        "limit_message": limit_message,
        "accepted": accepted,
        "not_sendable": [],
        "skipped_duplicates": skipped_duplicates,
        "ids": ids,
    }
