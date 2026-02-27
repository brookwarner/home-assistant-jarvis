from __future__ import annotations
import logging
from typing import Callable, Awaitable
from aiohttp import web

logger = logging.getLogger(__name__)


def make_app(on_event: Callable[[dict], Awaitable[None]]) -> web.Application:
    app = web.Application()

    async def handle_alert(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON")

        if "message" not in data:
            return web.Response(status=400, text="Missing 'message' field")

        logger.info(f"Received HA event: {data.get('title', '')} — {data.get('message', '')}")
        await on_event(data)
        return web.json_response({"status": "ok"})

    app.router.add_post("/alert", handle_alert)
    return app


async def start_server(on_event: Callable[[dict], Awaitable[None]], port: int) -> web.AppRunner:
    app = make_app(on_event)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info(f"Webhook server listening on localhost:{port}")
    return runner
