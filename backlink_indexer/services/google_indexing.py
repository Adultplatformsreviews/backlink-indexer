from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import SA_PATH

SCOPES = ["https://www.googleapis.com/auth/indexing"]
INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def _file_service_account() -> dict[str, Any] | None:
    path = Path(SA_PATH)
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(info, dict) and info.get("private_key") and info.get("client_email"):
        return info
    return None


def service_account_info() -> dict[str, Any] | None:
    """Service-account JSON stored in Postgres/SQLite, then the local file."""
    try:
        from ..database import get_setting

        raw = get_setting("google_sa_json") or ""
        if raw.strip():
            info = json.loads(raw)
            if isinstance(info, dict) and info.get("private_key") and info.get("client_email"):
                return info
    except Exception:
        pass
    return _file_service_account()


def service_account_present() -> bool:
    return service_account_info() is not None


def access_token_for(info: dict[str, Any]) -> str:
    """Bearer token for one service account. Tests may pass _access_token."""
    preset = (info or {}).get("_access_token")
    if preset:
        return str(preset)
    from google.oauth2 import service_account
    import google.auth.transport.requests

    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token or ""


async def publish_notification(
    client: Any,
    access_token: str,
    url: str,
    notification_type: str = "URL_UPDATED",
) -> dict[str, Any]:
    """POST URL_UPDATED to the Indexing API. Success is not an index confirmation."""
    if notification_type not in {"URL_UPDATED", "URL_DELETED"}:
        notification_type = "URL_UPDATED"
    if not access_token:
        return {"ok": False, "status": None, "detail": "Missing Google access token."}
    try:
        response = await client.post(
            INDEXING_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"url": url, "type": notification_type},
            timeout=30,
        )
    except Exception as exc:
        return {"ok": False, "status": None, "detail": f"Google Indexing API request failed: {exc}"}
    body = (response.text or "")[:800]
    ok = response.status_code in {200, 201}
    return {
        "ok": ok,
        "status": response.status_code,
        "detail": f"HTTP {response.status_code}" + (f": {body}" if body else ""),
        "response_text": body,
    }


def publish_url(url: str, notification_type: str = "URL_UPDATED") -> dict[str, Any]:
    """Publish a URL via the Google Indexing API.

    This only works for URLs on a site that the service account can access
    in Search Console (typically the owner's verified property). It cannot
    be used to request indexing of third-party backlink pages.
    """
    info = service_account_info()
    if not info:
        return {
            "ok": False,
            "detail": "No Google service-account JSON is uploaded in Settings.",
        }
    if notification_type not in {"URL_UPDATED", "URL_DELETED"}:
        notification_type = "URL_UPDATED"
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        return {"ok": False, "detail": f"Google API libraries are not installed: {exc}"}

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
        service = build("indexing", "v3", credentials=credentials, cache_discovery=False)
        body = {"url": url, "type": notification_type}
        result = service.urlNotifications().publish(body=body).execute()
        return {"ok": True, "detail": "Submitted via Google Indexing API.", "response": result}
    except Exception as exc:
        name = type(exc).__name__
        message = str(exc)
        # HttpError includes the JSON body which is useful.
        try:
            from googleapiclient.errors import HttpError

            if isinstance(exc, HttpError) and exc.content:
                message = exc.content.decode("utf-8", errors="replace")[:800]
        except Exception:
            pass
        return {
            "ok": False,
            "detail": (
                f"{name}: {message}. The Indexing API only accepts URLs on "
                "properties the service account can access in Search Console, "
                "and is officially intended for job postings and livestream pages."
            ),
        }
