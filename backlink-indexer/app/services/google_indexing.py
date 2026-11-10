from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import SA_PATH

SCOPES = ["https://www.googleapis.com/auth/indexing"]
INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def service_account_present() -> bool:
    return Path(SA_PATH).is_file() and Path(SA_PATH).stat().st_size > 0


def publish_url(url: str, notification_type: str = "URL_UPDATED") -> dict[str, Any]:
    """Publish a URL via the Google Indexing API.

    This only works for URLs on a site that the service account can access
    in Search Console (typically the owner's verified property). It cannot
    be used to request indexing of third-party backlink pages.
    """
    if not service_account_present():
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
        credentials = service_account.Credentials.from_service_account_file(
            str(SA_PATH), scopes=SCOPES
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
