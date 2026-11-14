"""Vercel entrypoint.

The real app is imported inside try/except. If that import fails, this module
still exposes ``app``: a pure ASGI callable with no third-party imports. Every
HTTP path, including /health, returns 500 and the traceback as plain text.
"""

from __future__ import annotations

import traceback


def _fallback_app(error_text: str):
    payload = error_text.encode("utf-8", errors="replace")

    async def app(scope, receive, send):
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                kind = message.get("type")
                if kind == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif kind == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope.get("type") != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    return app


try:
    from backlink_indexer.main import app as app
except Exception:
    app = _fallback_app(traceback.format_exc())
