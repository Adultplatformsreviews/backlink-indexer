from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from .database import get_conn, get_setting, insert_id, tx, utcnow
from .limits import is_unlimited_email, normalize_email, operator_email_for_username

_ITERATIONS = 120_000
_SCRYPT_N = 2**14


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        if scheme == "scrypt":
            digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=8, p=1)
        elif scheme == "pbkdf2":
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
        else:
            return False
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _user_row(row: Optional[dict]) -> Optional[dict]:
    """Admin and VIP follow the stored email on every read, not the signup order."""
    if not row:
        return None
    data = dict(row)
    raw_email = data.get("email") or ""
    email = normalize_email(raw_email)
    if not email:
        email = operator_email_for_username(data.get("username") or "")
    data["email"] = email
    operator = is_unlimited_email(email)
    data["unlimited"] = operator
    if operator:
        if raw_email != email or not data.get("is_admin") or data.get("plan_name") != "VIP":
            with tx() as conn:
                conn.execute(
                    "UPDATE users SET email = ?, is_admin = 1, plan_name = 'VIP' WHERE id = ?",
                    (email, data["id"]),
                )
        data["is_admin"] = 1
        data["plan_name"] = "VIP"
    else:
        if data.get("is_admin") or data.get("plan_name") == "VIP":
            with tx() as conn:
                conn.execute(
                    """UPDATE users SET is_admin = 0,
                       plan_name = CASE WHEN plan_name = 'VIP' THEN 'Free' ELSE plan_name END
                       WHERE id = ?""",
                    (data["id"],),
                )
        data["is_admin"] = 0
        if data.get("plan_name") == "VIP":
            data["plan_name"] = "Free"
    data["is_admin"] = bool(data.get("is_admin"))
    data["disabled"] = bool(data.get("disabled"))
    return data


def promote_operator_accounts() -> int:
    """Upgrade every existing row whose email is an operator address.

    Blank-email rows are filled only when the username is that address's
    local part. A different stored email is left as a normal account.
    """
    rows = get_conn().execute("SELECT * FROM users").fetchall()
    changed = 0
    for row in rows:
        before = (
            normalize_email(row["email"] or ""),
            int(row["is_admin"] or 0),
            row["plan_name"] or "",
        )
        user = _user_row(row)
        if not user:
            continue
        after = (user["email"], 1 if user["is_admin"] else 0, user["plan_name"])
        if after != before:
            changed += 1
    return changed


def get_user(user_id: int) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_row(row)


def get_user_by_username(username: str) -> Optional[dict]:
    key = (username or "").strip().lower()
    row = get_conn().execute("SELECT * FROM users WHERE username = ?", (key,)).fetchone()
    if row is None and "@" in key:
        row = get_conn().execute(
            "SELECT * FROM users WHERE lower(trim(email)) = ?",
            (key,),
        ).fetchone()
    return _user_row(row)


def get_user_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    row = get_conn().execute(
        "SELECT * FROM users WHERE lower(trim(email)) = ?",
        (email,),
    ).fetchone()
    return _user_row(row)


def create_user(username: str, password: str, email: str = "") -> tuple[Optional[dict], str]:
    username = (username or "").strip().lower()
    email = (email or "").strip().lower()
    if len(username) < 3 or len(username) > 40 or not username.replace("_", "").isalnum():
        return None, "Username must be 3–40 letters, numbers, or underscores."
    if "@" not in email or len(email) > 200:
        return None, "Enter a valid email address."
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters."
    if get_user_by_username(username):
        return None, "That username is already taken."
    if get_user_by_email(email):
        return None, "That email is already registered."
    vip = is_unlimited_email(email)
    try:
        tokens = max(0, int(get_setting("signup_tokens") or "100"))
    except ValueError:
        tokens = 100
    now = utcnow()
    with tx() as conn:
        user_id = insert_id(
            conn,
            """INSERT INTO users
               (username, password_hash, plan_name, plan_until, token_balance,
                daily_speed_cap, project_limit, is_admin, created_at, email, disabled)
               VALUES (?, ?, ?, '', ?, 100, 5, ?, ?, ?, 0)""",
            (
                username,
                hash_password(password),
                "VIP" if vip else "Free",
                tokens,
                1 if vip else 0,
                now,
                email,
            ),
        )
    return get_user(user_id), ""


def list_users() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM users ORDER BY id").fetchall()
    return [user for row in rows if (user := _user_row(row))]


def set_user_password(user_id: int, password: str) -> str:
    if len(password or "") < 8:
        return "Password must be at least 8 characters."
    if not get_user(user_id):
        return "User not found."
    with tx() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    return ""


def set_user_disabled(user_id: int, disabled: bool) -> str:
    user = get_user(user_id)
    if not user:
        return "User not found."
    if disabled and is_unlimited_email(user.get("email") or ""):
        return "The operator account cannot be disabled."
    with tx() as conn:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if disabled else 0, user_id))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return ""


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with tx() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, utcnow()),
        )
    return token


def user_from_session(token: str) -> Optional[dict]:
    if not token:
        return None
    row = get_conn().execute(
        """SELECT u.* FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token = ?""",
        (token,),
    ).fetchone()
    user = _user_row(row)
    if user and user.get("disabled"):
        return None
    return user


def delete_session(token: str) -> None:
    if not token:
        return
    with tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def add_tokens(user_id: int, amount: int) -> Optional[dict]:
    amount = int(amount)
    if amount == 0:
        return get_user(user_id)
    with tx() as conn:
        if amount < 0:
            cur = conn.execute(
                """UPDATE users SET token_balance = token_balance + ?
                   WHERE id = ? AND token_balance + ? >= 0""",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                return None
        else:
            conn.execute(
                "UPDATE users SET token_balance = token_balance + ? WHERE id = ?",
                (amount, user_id),
            )
    return get_user(user_id)


def rotate_api_key(user_id: int) -> str:
    raw = "bi_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = utcnow()
    with tx() as conn:
        conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
        insert_id(
            conn,
            """INSERT INTO api_keys (user_id, prefix, key_hash, created_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, raw[:7], digest, now),
        )
    return raw


def revoke_api_key(user_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))


def api_key_prefix(user_id: int) -> str:
    row = get_conn().execute(
        "SELECT prefix FROM api_keys WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["prefix"] if row else ""


def user_from_api_key(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    row = get_conn().execute(
        """SELECT u.* FROM api_keys k
           JOIN users u ON u.id = k.user_id
           WHERE k.key_hash = ?""",
        (digest,),
    ).fetchone()
    user = _user_row(row)
    if user and user.get("disabled"):
        return None
    return user
