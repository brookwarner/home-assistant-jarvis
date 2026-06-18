import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import TestClient, TestServer
import aiohttp

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk")]:
        monkeypatch.setenv(k, v)

async def test_alert_endpoint_calls_triage():
    from jarvis.webhook_server import make_app

    on_event = AsyncMock()
    app = make_app(on_event)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/alert",
            json={"title": "Security", "message": "Door opened", "entity_id": ""},
        )
        assert resp.status == 200

    on_event.assert_called_once()
    call_args = on_event.call_args[0][0]
    assert call_args["message"] == "Door opened"

async def test_alert_endpoint_rejects_bad_json():
    from jarvis.webhook_server import make_app

    on_event = AsyncMock()
    app = make_app(on_event)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/alert",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
