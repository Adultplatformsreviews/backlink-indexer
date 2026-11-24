"""Daily credit allowance: refill to a target once per Asia/Karachi day."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlink_indexer.allowance import (
    ALLOWANCE_TIMEZONE,
    DEFAULT_DAILY_CREDIT_EMAILS,
    LEDGER_KIND,
    allowance_day,
    apply_configured_allowances,
    apply_daily_allowance,
    daily_credit_allowances,
    parse_daily_credit_emails,
)
from backlink_indexer.auth import get_user
from backlink_indexer.credits import settle, unit_cost
from backlink_indexer.database import close_conn, get_conn, get_setting, insert_id, set_setting, tx, utcnow
from tests.test_app import signup


@pytest.fixture
def client():
    from backlink_indexer.main import app

    with TestClient(app) as test_client:
        conn = get_conn()
        for table in (
            "credit_ledger",
            "cron_leases",
            "events",
            "serp_cache",
            "hub_pages",
            "hub_domains",
            "connections",
            "submission_log",
            "urls",
            "batches",
            "notifications",
            "tickets",
            "api_keys",
            "sessions",
            "projects",
            "owned_hosts",
            "daily_usage",
            "login_codes",
            "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        close_conn()
        yield test_client

TARGET = "jhonnymorris96@gmail.com"
DAY_ONE = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)  # 17:00 in Karachi, still Nov 24
DAY_TWO = datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)  # 00:00 in Karachi, Nov 25
DAY_THREE = DAY_TWO + timedelta(days=1)


def test_allowance_day_rolls_at_karachi_midnight():
    assert ALLOWANCE_TIMEZONE == "Asia/Karachi"
    assert allowance_day(datetime(2026, 11, 24, 18, 59, tzinfo=timezone.utc)) == "2026-11-24"
    assert allowance_day(DAY_TWO) == "2026-11-25"
    assert allowance_day(datetime(2026, 11, 24, 18, 59)) == "2026-11-24"


def test_parser_and_default(monkeypatch):
    monkeypatch.delenv("DAILY_CREDIT_EMAILS", raising=False)
    assert daily_credit_allowances() == {TARGET: 100}
    assert DEFAULT_DAILY_CREDIT_EMAILS == "jhonnymorris96@gmail.com:100"

    monkeypatch.setenv("DAILY_CREDIT_EMAILS", "   ")
    assert daily_credit_allowances() == {TARGET: 100}

    parsed = parse_daily_credit_emails(
        "  JhonnyMorris96@gmail.com:100, other@example.com:40 , bad, nope:x, "
        "zero@example.com:0, huge@example.com:1000001, spaced@example.com: 15 "
    )
    assert parsed == {
        TARGET: 100,
        "other@example.com": 40,
        "spaced@example.com": 15,
    }
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", "other@example.com:25,other@example.com:40")
    assert daily_credit_allowances() == {"other@example.com": 40}
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", "not-an-email")
    assert daily_credit_allowances() == {}


def _user_id(username: str) -> int:
    user_id = int(get_conn().execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"])
    close_conn()
    return user_id


def _balance(user_id: int) -> int:
    balance = int(get_conn().execute("SELECT token_balance FROM users WHERE id = ?", (user_id,)).fetchone()["token_balance"])
    close_conn()
    return balance


def _prepare(client, balance: int, username: str = "jhonny", email: str = TARGET) -> int:
    signup(client, username, "password1", email=email)
    user_id = _user_id(username)
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = ? WHERE id = ?", (balance, user_id))
        conn.execute("DELETE FROM credit_ledger WHERE user_id = ?", (user_id,))
    close_conn()
    return user_id


def _ledger(user_id: int) -> list[dict]:
    rows = get_conn().execute(
        """SELECT day, kind, amount, balance_before, balance_after, target, email
           FROM credit_ledger WHERE user_id = ? ORDER BY day""",
        (user_id,),
    ).fetchall()
    close_conn()
    return [dict(row) for row in rows]


def test_refill_is_idempotent_and_does_not_stack(client, monkeypatch):
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100")
    user_id = _prepare(client, 40)

    first = apply_daily_allowance(user_id, TARGET, now=DAY_ONE)
    assert first["applied"] is True
    assert first["reason"] == "refilled"
    assert first["granted"] == 60
    assert first["balance"] == 100
    assert _balance(user_id) == 100
    rows = _ledger(user_id)
    assert rows == [
        {
            "day": "2026-11-24",
            "kind": LEDGER_KIND,
            "amount": 60,
            "balance_before": 40,
            "balance_after": 100,
            "target": 100,
            "email": TARGET,
        }
    ]

    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 10 WHERE id = ?", (user_id,))
    close_conn()
    second = apply_daily_allowance(user_id, TARGET, now=DAY_ONE)
    assert second["applied"] is False
    assert second["reason"] == "already_applied"
    assert second["granted"] == 0
    assert second["recorded_amount"] == 60
    assert _balance(user_id) == 10
    assert len(_ledger(user_id)) == 1

    # Unused credits are not added on top of the next day's target.
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 80 WHERE id = ?", (user_id,))
    close_conn()
    third = apply_daily_allowance(user_id, TARGET, now=DAY_TWO)
    assert third["granted"] == 20
    assert third["balance"] == 100
    assert _balance(user_id) == 100
    stayed = apply_daily_allowance(user_id, TARGET, now=DAY_TWO)
    assert stayed["granted"] == 0
    assert _balance(user_id) == 100

    fourth = apply_daily_allowance(user_id, TARGET, now=DAY_THREE)
    assert fourth["reason"] == "already_at_target"
    assert fourth["granted"] == 0
    assert _balance(user_id) == 100
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 25 WHERE id = ?", (user_id,))
    close_conn()
    assert apply_daily_allowance(user_id, TARGET, now=DAY_THREE)["granted"] == 0
    assert _balance(user_id) == 25


def test_balance_above_target_is_not_reduced(client, monkeypatch):
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100")
    user_id = _prepare(client, 150)
    result = apply_daily_allowance(user_id, TARGET, now=DAY_ONE)
    assert result["reason"] == "already_at_target"
    assert result["granted"] == 0
    assert _balance(user_id) == 150
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 12 WHERE id = ?", (user_id,))
    close_conn()
    assert apply_daily_allowance(user_id, TARGET, now=DAY_ONE)["granted"] == 0
    assert _balance(user_id) == 12


def test_signup_later_and_login_apply_once(client, monkeypatch):
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", "later@example.com:100")
    missing = apply_configured_allowances()
    assert missing["granted"] == 0
    assert missing["accounts"][0]["reason"] == "no_account"
    assert missing["accounts"][0]["email"] == "later@example.com"

    previous = get_setting("signup_tokens") or "100"
    set_setting("signup_tokens", "15")
    try:
        signup(client, "later", "password1", email="Later@Example.com")
    finally:
        set_setting("signup_tokens", previous)
    user_id = _user_id("later")
    assert _balance(user_id) == 100
    rows = _ledger(user_id)
    assert len(rows) == 1
    assert rows[0]["amount"] == 85
    assert rows[0]["balance_before"] == 15
    assert rows[0]["balance_after"] == 100
    assert rows[0]["email"] == "later@example.com"

    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 0 WHERE id = ?", (user_id,))
    close_conn()
    assert client.get("/app").status_code == 200
    assert _balance(user_id) == 0

    with tx() as conn:
        conn.execute("DELETE FROM credit_ledger WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET token_balance = 20 WHERE id = ?", (user_id,))
    close_conn()
    client.post("/logout")
    signed = client.post(
        "/login",
        data={"username": "later@example.com", "password": "password1"},
        follow_redirects=False,
    )
    assert signed.status_code == 303
    assert _balance(user_id) == 100
    plans = client.get("/app/plans")
    assert "refilled to at least 100" in plans.text
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 7 WHERE id = ?", (user_id,))
    close_conn()
    assert client.get("/app").status_code == 200
    assert _balance(user_id) == 7


def test_default_account_signup_does_not_stack_on_the_signup_grant(client, monkeypatch):
    monkeypatch.delenv("DAILY_CREDIT_EMAILS", raising=False)
    signup(client, "jhonny", "password1", email="JhonnyMorris96@gmail.com")
    user_id = _user_id("jhonny")
    user = get_user(user_id)
    assert user["email"] == TARGET
    assert user["unlimited"] is False
    assert unit_cost(user) == 1
    assert _balance(user_id) == 100
    rows = _ledger(user_id)
    assert len(rows) == 1
    assert rows[0]["amount"] == 0
    assert rows[0]["target"] == 100
    page = client.get("/app/plans")
    assert "refilled to at least 100" in page.text
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 30 WHERE id = ?", (user_id,))
    close_conn()
    assert client.get("/app/plans").status_code == 200
    assert _balance(user_id) == 30


def test_cron_route_refills_once_and_leaves_other_accounts(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100,other@example.com:50")
    user_id = _prepare(client, 40)
    signup(client, "plain", "password1", email="plain@example.com")
    plain_id = _user_id("plain")
    plain_before = _balance(plain_id)

    denied = client.post("/api/cron/daily-credits")
    assert denied.status_code == 401
    first = client.post("/api/cron/daily-credits", headers={"Authorization": "Bearer cron-secret"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True
    assert body["timezone"] == "Asia/Karachi"
    assert body["granted"] == 60
    assert _balance(user_id) == 100
    assert _balance(plain_id) == plain_before
    reasons = {item["email"]: item["reason"] for item in body["accounts"]}
    assert reasons[TARGET] == "refilled"
    assert reasons["other@example.com"] == "no_account"

    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 5 WHERE id = ?", (user_id,))
    close_conn()
    second = client.post("/api/cron/daily-credits", headers={"Authorization": "Bearer cron-secret"})
    assert second.status_code == 200
    assert second.json()["granted"] == 0
    assert _balance(user_id) == 5


def test_operator_sees_the_allowance_list(client, monkeypatch):
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100")
    signup(client, "boss", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    page = client.get("/app/users")
    assert TARGET in page.text
    assert "DAILY_CREDIT_EMAILS" in page.text
    assert "Asia/Karachi" in page.text


def test_unlimited_accounts_still_bypass_credit_charges(client, monkeypatch):
    monkeypatch.setenv("UNLIMITED_EMAILS", "muhammadzeeshanhaideroffical@gmail.com")
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100,muhammadzeeshanhaideroffical@gmail.com:100")
    signup(client, "boss", "password1", email="muhammadzeeshanhaideroffical@gmail.com")
    boss_id = _user_id("boss")
    boss = get_user(boss_id)
    assert boss["unlimited"] is True
    assert boss["is_admin"] is True
    assert unit_cost(boss) == 0
    with tx() as conn:
        conn.execute("UPDATE users SET token_balance = 9 WHERE id = ?", (boss_id,))
        conn.execute("DELETE FROM credit_ledger WHERE user_id = ?", (boss_id,))
    close_conn()
    refreshed = get_user(boss_id)
    assert refreshed["unlimited"] is True
    assert unit_cost(refreshed) == 0
    assert refreshed["token_balance"] == 100

    signup(client, "jhonny", "password1", email=TARGET)
    person = get_user(_user_id("jhonny"))
    assert person["unlimited"] is False
    assert unit_cost(person) == 1


def test_refill_leaves_reservations_and_refunds_intact(client, monkeypatch):
    monkeypatch.setenv("DAILY_CREDIT_EMAILS", f"{TARGET}:100")
    user_id = _prepare(client, 20)
    now = utcnow()
    with tx() as conn:
        project_id = insert_id(
            conn,
            "INSERT INTO projects (user_id, name, created_at, updated_at) VALUES (?, 'Held', ?, ?)",
            (user_id, now, now),
        )
        url_id = insert_id(
            conn,
            """INSERT INTO urls
               (project_id, url, created_at, updated_at, credits_state, credits_held, status)
               VALUES (?, 'https://example.com/held', ?, ?, 'reserved', 5, 'submitted')""",
            (project_id, now, now),
        )
    close_conn()

    result = apply_daily_allowance(user_id, TARGET, now=DAY_ONE)
    assert result["granted"] == 80
    assert _balance(user_id) == 100
    held = get_conn().execute(
        "SELECT credits_state, credits_held FROM urls WHERE id = ?",
        (url_id,),
    ).fetchone()
    close_conn()
    assert held["credits_state"] == "reserved"
    assert int(held["credits_held"]) == 5

    assert settle(url_id, "refunded", "Window elapsed.") == 5
    assert _balance(user_id) == 105
    state = get_conn().execute("SELECT credits_state FROM urls WHERE id = ?", (url_id,)).fetchone()
    close_conn()
    assert state["credits_state"] == "refunded"
    again = apply_daily_allowance(user_id, TARGET, now=DAY_ONE)
    assert again["granted"] == 0
    assert _balance(user_id) == 105
    assert len(_ledger(user_id)) == 1


def test_schedules_are_daily_and_call_the_new_route():
    vercel = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    paths = {item["path"]: item["schedule"] for item in vercel["crons"]}
    assert paths["/api/cron/daily-credits"] == "5 19 * * *"
    for schedule in paths.values():
        minute, hour, day, month, weekday = schedule.split()
        assert minute.isdigit() and hour.isdigit()
        assert "/" not in schedule
        assert day == month == weekday == "*"

    workflow = Path(".github/workflows/indexer-cron.yml").read_text(encoding="utf-8")
    assert 'cron: "5 19 * * *"' in workflow
    assert "CRON_PATH: /api/cron/daily-credits" in workflow
    assert "inputs.job == 'daily-credits'" in workflow
