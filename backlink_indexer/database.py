from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .config import DATA_DIR, DB_PATH, DEFAULT_TARGET_DOMAIN

_local = threading.local()
_schema_ready = False
_db_status = {"persistent": False, "ready": False, "error": "", "env": ""}

# Vercel Storage / Neon injects different names depending on the integration.
# Pooled URLs come first because this app runs as short-lived serverless functions.
_DATABASE_ENV_KEYS = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "DATABASE_URL_UNPOOLED",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_URL_NO_SSL",
    "NEON_DATABASE_URL",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_url(value: str) -> str:
    return (value or "").strip().strip('"').strip("'")


def resolve_database_url() -> tuple[str, str]:
    """Return (url, env var name). Empty url means SQLite."""
    for key in _DATABASE_ENV_KEYS:
        url = _clean_url(os.environ.get(key) or "")
        if url.startswith("postgres://") or url.startswith("postgresql://"):
            return url, key
    return "", ""


def database_url() -> str:
    return resolve_database_url()[0]


def database_env_name() -> str:
    return resolve_database_url()[1]


def uses_postgres() -> bool:
    return bool(database_url())


def persistent_db() -> bool:
    return bool(_db_status["persistent"] and _db_status["ready"] and not _db_status["error"])


def bootstrap_db() -> dict[str, Any]:
    """Open the configured database and create tables. Never raises."""
    try:
        get_conn()
    except Exception as exc:
        _db_status["persistent"] = False
        _db_status["ready"] = False
        _db_status["error"] = _db_status.get("error") or type(exc).__name__
    try:
        persistent = persistent_db()
        return {
            "persistent_db": persistent,
            "ephemeral_storage": not persistent,
            "database_env": _db_status.get("env") or "",
            "database_error": _db_status.get("error") or "",
        }
    except Exception as exc:
        return {
            "persistent_db": False,
            "ephemeral_storage": True,
            "database_env": "",
            "database_error": type(exc).__name__,
        }


class Result:
    def __init__(self, rows: list[dict], rowcount: int = 0, lastrowid: Optional[int] = None):
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Optional[dict]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows)


def _adapt(sql: str, postgres: bool) -> str:
    if not postgres:
        return sql
    parts = re.split(r"('(?:''|[^'])*')", sql)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
            continue
        part = part.replace("?", "%s")
        part = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", part, flags=re.I)
        out.append(part)
    return "".join(out)


class DB:
    def __init__(self, raw: Any, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Result:
        sql = _adapt(sql, self.postgres)
        values = tuple(params or ())
        if self.postgres:
            cur = self.raw.execute(sql, values)
            rows: list[dict] = []
            if cur.description:
                rows = [dict(row) for row in cur.fetchall()]
            return Result(rows, cur.rowcount or 0)
        cur = self.raw.execute(sql, values)
        rows = []
        if cur.description:
            rows = [dict(row) for row in cur.fetchall()]
        return Result(rows, cur.rowcount if cur.rowcount is not None else 0, cur.lastrowid)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def _connect() -> DB:
    url, env_name = resolve_database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=15)
        raw.execute("SET search_path TO public")
        _db_status["env"] = env_name
        return DB(raw, True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    raw.execute("PRAGMA busy_timeout = 5000")
    return DB(raw, False)


def get_conn() -> DB:
    conn = getattr(_local, "conn", None)
    if conn is None:
        try:
            conn = _connect()
        except Exception as exc:
            _db_status["persistent"] = False
            _db_status["ready"] = False
            _db_status["error"] = type(exc).__name__
            _db_status["env"] = database_env_name()
            raise
        _local.conn = conn
    global _schema_ready
    if not _schema_ready:
        try:
            init_schema(conn)
        except Exception as exc:
            _db_status["persistent"] = False
            _db_status["ready"] = False
            _db_status["error"] = type(exc).__name__
            _db_status["env"] = database_env_name() if conn.postgres else ""
            raise
        _schema_ready = True
        _db_status["ready"] = True
        _db_status["error"] = ""
        _db_status["persistent"] = bool(conn.postgres)
        _db_status["env"] = database_env_name() if conn.postgres else ""
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def insert_id(conn: DB, sql: str, params: Iterable[Any] = ()) -> int:
    if conn.postgres and "returning" not in sql.lower():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
        row = conn.execute(sql, params).fetchone()
        return int(row["id"]) if row else 0
    result = conn.execute(sql, params)
    if conn.postgres:
        row = result.fetchone()
        return int(row["id"]) if row else 0
    return int(result.lastrowid or 0)


def row_to_dict(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: Iterable[dict]) -> list[dict]:
    return [dict(r) for r in rows]


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    plan_name TEXT NOT NULL DEFAULT 'Free',
    plan_until TEXT NOT NULL DEFAULT '',
    token_balance INTEGER NOT NULL DEFAULT 0,
    daily_speed_cap INTEGER NOT NULL DEFAULT 100,
    project_limit INTEGER NOT NULL DEFAULT 5,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    target_domain TEXT NOT NULL DEFAULT 'feetfinder.com',
    include_subdomains INTEGER NOT NULL DEFAULT 1,
    project_type TEXT NOT NULL DEFAULT 'indexing',
    daily_speed INTEGER NOT NULL DEFAULT 10,
    run_status TEXT NOT NULL DEFAULT 'running',
    sent_today INTEGER NOT NULL DEFAULT 0,
    sent_on TEXT NOT NULL DEFAULT '',
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
    google_state TEXT NOT NULL DEFAULT 'unknown',
    yandex_state TEXT NOT NULL DEFAULT 'unknown',
    bing_state TEXT NOT NULL DEFAULT 'unknown',
    queue_status TEXT NOT NULL DEFAULT 'queued',
    queue_note TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS submission_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id INTEGER REFERENCES urls(id) ON DELETE SET NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    urls_added INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS login_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_urls_project ON urls(project_id);
CREATE INDEX IF NOT EXISTS idx_urls_queue ON urls(project_id, queue_status);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

_POSTGRES_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        plan_name TEXT NOT NULL DEFAULT 'Free',
        plan_until TEXT NOT NULL DEFAULT '',
        token_balance INTEGER NOT NULL DEFAULT 0,
        daily_speed_cap INTEGER NOT NULL DEFAULT 100,
        project_limit INTEGER NOT NULL DEFAULT 5,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS projects (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        target_domain TEXT NOT NULL DEFAULT 'feetfinder.com',
        include_subdomains INTEGER NOT NULL DEFAULT 1,
        project_type TEXT NOT NULL DEFAULT 'indexing',
        daily_speed INTEGER NOT NULL DEFAULT 10,
        run_status TEXT NOT NULL DEFAULT 'running',
        sent_today INTEGER NOT NULL DEFAULT 0,
        sent_on TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS batches (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS urls (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        batch_id BIGINT REFERENCES batches(id) ON DELETE SET NULL,
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
        google_state TEXT NOT NULL DEFAULT 'unknown',
        yandex_state TEXT NOT NULL DEFAULT 'unknown',
        bing_state TEXT NOT NULL DEFAULT 'unknown',
        queue_status TEXT NOT NULL DEFAULT 'queued',
        queue_note TEXT NOT NULL DEFAULT '',
        last_checked_at TEXT,
        last_submitted_at TEXT,
        last_index_check_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, url)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS owned_hosts (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        host TEXT NOT NULL UNIQUE,
        notes TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS submission_log (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        url_id BIGINT REFERENCES urls(id) ON DELETE SET NULL,
        method TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS daily_usage (
        user_id BIGINT NOT NULL,
        day TEXT NOT NULL,
        urls_added INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )""",
    """CREATE TABLE IF NOT EXISTS login_codes (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        email TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        purpose TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_urls_project ON urls(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_urls_queue ON urls(project_id, queue_status)",
    "CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
]

_USER_COLUMNS = {
    "email": "TEXT NOT NULL DEFAULT ''",
    "disabled": "INTEGER NOT NULL DEFAULT 0",
}

_PROJECT_COLUMNS = {
    "user_id": "BIGINT" if False else "INTEGER",
    "project_type": "TEXT NOT NULL DEFAULT 'indexing'",
    "daily_speed": "INTEGER NOT NULL DEFAULT 10",
    "run_status": "TEXT NOT NULL DEFAULT 'running'",
    "sent_today": "INTEGER NOT NULL DEFAULT 0",
    "sent_on": "TEXT NOT NULL DEFAULT ''",
    "notes": "TEXT NOT NULL DEFAULT ''",
    "tags": "TEXT NOT NULL DEFAULT ''",
    "use_google": "INTEGER NOT NULL DEFAULT 1",
    "use_bing": "INTEGER NOT NULL DEFAULT 1",
    "use_yandex": "INTEGER NOT NULL DEFAULT 1",
    "use_indexnow": "INTEGER NOT NULL DEFAULT 1",
}

_URL_COLUMNS = {
    "google_state": "TEXT NOT NULL DEFAULT 'unknown'",
    "yandex_state": "TEXT NOT NULL DEFAULT 'unknown'",
    "bing_state": "TEXT NOT NULL DEFAULT 'unknown'",
    "queue_status": "TEXT NOT NULL DEFAULT 'queued'",
    "queue_note": "TEXT NOT NULL DEFAULT ''",
    "hold_until": "TEXT NOT NULL DEFAULT ''",
}


def _columns(conn: DB, table: str) -> set[str]:
    if conn.postgres:
        rows = conn.execute(
            """SELECT column_name AS name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = ?""",
            (table,),
        ).fetchall()
        return {r["name"] for r in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _add_columns(conn: DB, table: str, columns: dict[str, str]) -> None:
    if not _columns(conn, table):
        return
    existing = _columns(conn, table)
    for name, decl in columns.items():
        if name in existing:
            continue
        pg_decl = decl.replace("INTEGER", "BIGINT") if name == "user_id" and conn.postgres else decl
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {pg_decl}")


def init_schema(conn: DB) -> None:
    if conn.postgres:
        for statement in _POSTGRES_STATEMENTS:
            conn.execute(statement)
    else:
        conn.raw.executescript(_SQLITE_SCHEMA)
    _add_columns(conn, "users", _USER_COLUMNS)
    _add_columns(conn, "projects", _PROJECT_COLUMNS)
    _add_columns(conn, "urls", _URL_COLUMNS)
    _seed_defaults(conn)
    conn.commit()


def init_db() -> dict[str, Any]:
    """Create the public schema when Postgres is configured. Returns the persistence report."""
    return bootstrap_db()


def _seed_defaults(conn: DB) -> None:
    defaults = {
        "public_base_url": "",
        "indexnow_key": "",
        "serp_provider": "serpapi",
        "serp_api_key": "",
        "serp_endpoint": "",
        "websub_hub": "https://pubsubhubbub.appspot.com/",
        "hub_include": "all",
        "default_target_domain": DEFAULT_TARGET_DOMAIN,
        "include_subdomains": "1",
        "tokens_per_url": "1",
        "signup_tokens": "100",
        "google_sa_json": "",
        "bing_webmaster_api_key": "",
        "bing_site_url": "",
        "yandex_webmaster_token": "",
        "yandex_webmaster_user_id": "",
        "yandex_webmaster_host_id": "",
        "yandex_site_host": "",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (key, value),
        )


def get_setting(key: str, default: str = "") -> str:
    row = get_conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    return str(row["value"])


def get_settings() -> dict[str, str]:
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: "" if r["value"] is None else str(r["value"]) for r in rows}


def set_setting(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def set_settings(values: dict[str, Any]) -> None:
    with tx() as conn:
        for key, value in values.items():
            if value is None:
                value = ""
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, str(value)),
            )
