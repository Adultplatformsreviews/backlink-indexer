import asyncio

from fastapi.testclient import TestClient

from vercel_app import _fallback_app, app


def test_entrypoint_loads_the_real_app():
    assert app.title == "Backlink Indexer"


def test_fallback_returns_traceback_on_every_path():
    application = _fallback_app("Traceback (most recent call last):\nImportError: fastapi\n")
    client = TestClient(application)
    for path in ("/health", "/app", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 500
        assert "ImportError: fastapi" in response.text
        assert response.headers["content-type"].startswith("text/plain")


def test_fallback_lifespan_completes():
    application = _fallback_app("boom")
    messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def run():
        sent = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message["type"])

        await application({"type": "lifespan"}, receive, send)
        return sent

    assert asyncio.run(run()) == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
