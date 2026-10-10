"""Connects outbound to the website's WebSocket hub as "the teammate
system" (see website/docs/message-contract.md and website/CLAUDE.md's
"Open question 2"): the website's server broadcasts to every connected
client, so this is just another client on the same socket, reacting to
`transcript` messages and sending `display`/`state` messages back.

This replaces the old server.py, which wrongly assumed the Pi should
be an inbound HTTP server -- the real website is a WS broadcast hub
the Pi connects OUT to, matching the teammate's own stated design.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import aiohttp

from pi_app.audio_player import AudioPlayer
from pi_app.decision_service import DecisionService
from pi_app.esp32_link import EspLink
from pi_app.snacks import SnacksStore

RECONNECT_DELAY_S = 3.0
DISPENSE_OPEN_SECONDS = 4.0


class TeammateClient:
    def __init__(
        self,
        ws_url: str,
        decision_service: DecisionService,
        esp32_link: EspLink,
        audio_player: AudioPlayer,
        snacks_store: SnacksStore,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
        dispense_open_seconds: float = DISPENSE_OPEN_SECONDS,
        logger: logging.Logger | None = None,
    ):
        self._ws_url = ws_url
        self._decision_service = decision_service
        self._esp32_link = esp32_link
        self._audio_player = audio_player
        self._snacks_store = snacks_store
        self._reconnect_delay_s = reconnect_delay_s
        self._dispense_open_seconds = dispense_open_seconds
        self._logger = logger or logging.getLogger(__name__)

    async def run_forever(self):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await self._connect_and_listen(session)
                except (aiohttp.ClientError, ConnectionError, OSError) as e:
                    self._logger.warning(f"websocket connection lost/failed: {e}")
                await asyncio.sleep(self._reconnect_delay_s)

    async def _connect_and_listen(self, session: aiohttp.ClientSession):
        async with session.ws_connect(self._ws_url) as ws:
            self._logger.info(f"connected to {self._ws_url}")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self.handle_message(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break

    async def handle_message(self, ws, raw: str):
        """Reacts to one incoming websocket text frame. Messages this
        client doesn't care about (session-state, non-transcript types)
        are silently ignored, matching the broadcast-to-everyone design."""
        try:
            data = json.loads(raw)
        except ValueError:
            return

        if data.get("type") != "transcript":
            return

        text = (data.get("payload") or {}).get("text", "")
        if not text or not text.strip():
            return

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(
            None, self._decision_service.handle, request_id, text, self._snacks_store.current
        )

        snack = self._snacks_store.current.get(message.snack_id)
        snack_label = snack.label if snack else message.snack_id
        await ws.send_json({"type": "display", "payload": {"kind": "result", "snack": snack_label}})

        if message.audio_mp3:
            await ws.send_json({"type": "state", "payload": {"speaking": True}})
            await loop.run_in_executor(None, self._audio_player.play, message.audio_mp3)
            await ws.send_json({"type": "state", "payload": {"speaking": False}})

        # Only one physical mechanism exists right now (one trap door, no
        # per-slot routing -- see esp32_link.py), so every decision just
        # pulses it open then closed. This also serializes dispensing:
        # the next transcript isn't read off the socket until this
        # coroutine returns, which is fine since only one session can be
        # active on the website's side anyway.
        await loop.run_in_executor(None, self._esp32_link.open_trap_door)
        await asyncio.sleep(self._dispense_open_seconds)
        await loop.run_in_executor(None, self._esp32_link.close_trap_door)


def main():
    import os

    logging.basicConfig(level=logging.INFO)

    from pi_app.decision_client import DecisionClient
    from pi_app.dotenv import load_secrets_env
    from pi_app.esp32_link import NullEspLink, SerialEspLink
    from pi_app.request_budget import CharacterBudget, RequestBudget
    from pi_app.tts_client import TtsClient

    load_secrets_env()

    snacks_path = os.environ.get("SNACKS_CONFIG_PATH", "config/snacks.json")
    snacks_store = SnacksStore(snacks_path)

    decision_client = DecisionClient(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-5-nano"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    tts_client = TtsClient(
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
    )
    decision_service = DecisionService(
        decision_client,
        tts_client,
        RequestBudget(daily_limit=int(os.environ.get("LLM_DAILY_REQUEST_CAP", "300"))),
        CharacterBudget(daily_limit=int(os.environ.get("TTS_DAILY_CHAR_CAP", "20000"))),
    )

    esp32_port = os.environ.get("ESP32_SERIAL_PORT")
    esp32_link = SerialEspLink(esp32_port) if esp32_port else NullEspLink()

    client = TeammateClient(
        ws_url=os.environ.get("WEBSITE_WS_URL", "ws://localhost:3000"),
        decision_service=decision_service,
        esp32_link=esp32_link,
        audio_player=AudioPlayer(),
        snacks_store=snacks_store,
    )
    asyncio.run(client.run_forever())


if __name__ == "__main__":
    main()
