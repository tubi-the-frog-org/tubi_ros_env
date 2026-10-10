"""Minimal HTTP server: POST /speak takes the guest's transcribed text,
runs it through DecisionService, forwards the result to the ESP32
link, and returns it as JSON.

Scope note: this does NOT yet serve the guest-facing frontend (mic
capture page) -- that's still a separate concern, not built here. It
also doesn't yet have the rate limiting, CORS, or WebSocket /events
push that the original web_bridge design called for; those are still
open follow-ups, not silently dropped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from aiohttp import web

from pi_app.decision_service import DecisionService
from pi_app.esp32_link import EspLink
from pi_app.snacks import SnacksStore

MAX_TEXT_LEN = 300
RELOAD_POLL_INTERVAL_S = 2.0

logger = logging.getLogger(__name__)


def create_app(
    snacks_store: SnacksStore,
    decision_service: DecisionService,
    esp32_link: EspLink,
    web_bridge_token: str,
    reload_poll_interval_s: float = RELOAD_POLL_INTERVAL_S,
) -> web.Application:
    app = web.Application()
    app["snacks_store"] = snacks_store
    app["decision_service"] = decision_service
    app["esp32_link"] = esp32_link
    app["web_bridge_token"] = web_bridge_token
    app["reload_poll_interval_s"] = reload_poll_interval_s

    app.router.add_post("/speak", handle_speak)
    app.router.add_get("/healthz", handle_healthz)

    app.on_startup.append(_start_reload_poller)
    app.on_cleanup.append(_stop_reload_poller)
    return app


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_speak(request: web.Request) -> web.Response:
    token = request.app["web_bridge_token"]
    if token:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
        text = body["text"]
    except Exception:
        return web.json_response({"error": "expected JSON body with a 'text' field"}, status=400)

    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "'text' must be a non-empty string"}, status=400)
    if len(text) > MAX_TEXT_LEN:
        return web.json_response({"error": f"'text' exceeds {MAX_TEXT_LEN} chars"}, status=400)

    request_id = str(uuid.uuid4())
    snacks_store: SnacksStore = request.app["snacks_store"]
    decision_service: DecisionService = request.app["decision_service"]
    esp32_link: EspLink = request.app["esp32_link"]

    loop = asyncio.get_running_loop()
    message = await loop.run_in_executor(
        None, decision_service.handle, request_id, text, snacks_store.current
    )
    await loop.run_in_executor(None, esp32_link.send, message)

    return web.json_response({
        "request_id": message.request_id,
        "snack_id": message.snack_id,
        "text": message.reply_text,
        "audio_available": message.audio_mp3 is not None,
        "source": message.source,
    })


async def _reload_poll_loop(app: web.Application):
    snacks_store: SnacksStore = app["snacks_store"]
    interval = app["reload_poll_interval_s"]
    try:
        while True:
            await asyncio.sleep(interval)
            snacks_store.maybe_reload()
    except asyncio.CancelledError:
        pass


async def _start_reload_poller(app: web.Application):
    app["reload_task"] = asyncio.create_task(_reload_poll_loop(app))


async def _stop_reload_poller(app: web.Application):
    task = app.get("reload_task")
    if task:
        task.cancel()
        await task


def main():
    logging.basicConfig(level=logging.INFO)

    snacks_path = os.environ.get("SNACKS_CONFIG_PATH", "config/snacks.json")
    snacks_store = SnacksStore(snacks_path)

    from pi_app.decision_client import DecisionClient
    from pi_app.esp32_link import NullEspLink, SerialEspLink
    from pi_app.tts_client import TtsClient
    from pi_app.request_budget import CharacterBudget, RequestBudget

    decision_client = DecisionClient(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-5-nano"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    tts_client = TtsClient(
        base_url=os.environ.get("TTS_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("TTS_MODEL", "gpt-4o-mini-tts"),
        api_key=os.environ.get("TTS_API_KEY", ""),
    )
    decision_service = DecisionService(
        decision_client,
        tts_client,
        RequestBudget(daily_limit=int(os.environ.get("LLM_DAILY_REQUEST_CAP", "300"))),
        CharacterBudget(daily_limit=int(os.environ.get("TTS_DAILY_CHAR_CAP", "20000"))),
    )

    esp32_port = os.environ.get("ESP32_SERIAL_PORT")
    esp32_link = SerialEspLink(esp32_port) if esp32_port else NullEspLink()

    app = create_app(snacks_store, decision_service, esp32_link, os.environ.get("WEB_BRIDGE_TOKEN", ""))
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
