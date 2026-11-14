"""Outbound email via Resend or SMTP. Never includes a plaintext password."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from .services.secrets import setting_or_env

_OUTBOX: list[dict] = []


def _value(setting_key: str, env_name: str) -> str:
    value, _source = setting_or_env(setting_key, env_name)
    return value


def describe_mailer() -> tuple[bool, str]:
    email_from = _value("email_from", "EMAIL_FROM")
    resend = _value("resend_api_key", "RESEND_API_KEY")
    smtp_host = _value("smtp_host", "SMTP_HOST")
    if resend and email_from:
        return True, "Resend"
    if smtp_host and email_from:
        return True, "SMTP"
    return False, (
        "No email provider is configured. An administrator must set RESEND_API_KEY and EMAIL_FROM, "
        "or SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, and EMAIL_FROM, in the environment or Settings."
    )


def send_email(to: str, subject: str, text: str) -> tuple[bool, str]:
    ok, detail = describe_mailer()
    _OUTBOX.append({"to": to, "subject": subject, "text": text, "ok": ok, "detail": detail})
    if not ok:
        return False, detail
    email_from = _value("email_from", "EMAIL_FROM")
    resend = _value("resend_api_key", "RESEND_API_KEY")
    if resend:
        return _send_resend(resend, email_from, to, subject, text)
    return _send_smtp(email_from, to, subject, text)


def _send_resend(api_key: str, email_from: str, to: str, subject: str, text: str) -> tuple[bool, str]:
    import httpx

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": email_from, "to": [to], "subject": subject, "text": text},
            timeout=20,
        )
    except Exception as exc:
        return False, f"Resend request failed: {exc}"
    if response.status_code in {200, 201}:
        return True, "Sent with Resend."
    return False, f"Resend HTTP {response.status_code}: {(response.text or '')[:300]}"


def _send_smtp(email_from: str, to: str, subject: str, text: str) -> tuple[bool, str]:
    host = _value("smtp_host", "SMTP_HOST")
    port_raw = _value("smtp_port", "SMTP_PORT") or "587"
    user = _value("smtp_user", "SMTP_USER")
    password = _value("smtp_password", "SMTP_PASSWORD")
    try:
        port = int(port_raw)
    except ValueError:
        return False, "SMTP_PORT is not a number."
    message = EmailMessage()
    message["From"] = email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:
        return False, f"SMTP send failed: {exc}"
    return True, "Sent with SMTP."
