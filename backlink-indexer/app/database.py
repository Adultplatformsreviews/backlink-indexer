from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import DB_PATH, DEFAULT_TARGET_DOMAIN

_local = threading.local()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_domain TEXT NOT NULL DEFAULT 'feetfinder.com',
            include_subdomains INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
            url TEXT NOT NULL,
            final_url TEXT,
            http_status INTEGER,
            is_live INTEGER,
            fetch_error TEXT,
            content_type TEXT,
            meta_robots TEXT,
            x_robots_tag TEXT,
            noindex INTEGER,
            robots_disallow INTEGER,
            canonical TEXT,
            canonicalized_away INTEGER,
            has_link INTEGER,
            link_rel TEXT,
            link_count INTEGER,
            link_details TEXT,
            in_hub INTEGER NOT NULL DEFAULT 1,
            submission_status TEXT NOT NULL DEFAULT 'pending',
            indexnow_status TEXT,
            google_index_status TEXT,
            index_status TEXT NOT NULL DEFAULT 'unknown',
            index_source TEXT,
            index_note TEXT,
            last_checked_at TEXT,
            last_submitted_at TEXT,
            last_index_check_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, url)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS owned_hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL UNIQUE,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            total INTEGER NOT NULL DEFAULT 0,
            done_count INTEGER NOT NULL DEFAULT 0,
            ok_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS submission_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_id INTEGER REFERENCES urls(id) ON DELETE SET NULL,
            method TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_urls_project ON urls(project_id);
        CREATE INDEX IF NOT EXISTS idx_urls_batch ON urls(batch_id);
        CREATE INDEX IF NOT EXISTS idx_batches_project ON batches(project_id);
        """
    )
    conn.commit()
    _seed_defaults(conn)


def _seed_defaults(conn: sqlite3.Connection) -> None:
    now = utcnow()
    existing = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    if existing == 0:
        conn.execute(
            """INSERT INTO projects (name, target_domain, include_subdomains, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?)""",
            ("FeetFinder backlinks", DEFAULT_TARGET_DOMAIN, now, now),
        )
    defaults = {
        "public_base_url": "",
        "indexnow_key": "",
        "serp_provider": "serpapi",
        "serp_api_key": "",
        "serp_endpoint": "",
        "websub_hub": "https://pubsubhubbub.appspot.com/",
        "hub_include": "all",  # all | live | has_link
        "default_target_domain": DEFAULT_TARGET_DOMAIN,
        "include_subdomains": "1",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def get_setting(key: str, default: str = "") -> str:
    row = get_conn().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None or row["value"] is None:
        return default
    return row["value"]


def get_settings() -> dict[str, str]:
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] if r["value"] is not None else "" for r in rows}


def set_setting(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def set_settings(values: dict[str, Any]) -> None:
    with tx() as conn:
        for key, value in values.items():
            if value is None:
                value = ""
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
