"""Connects the Pi conversation service to the website's WebSocket hub.

`session:start` creates a conversation, `transcript` advances it, and
`session:end` clears it. Replies are spoken on every turn; the final turn also
sends a magic type and level display message.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid

import aiohttp

from pi_app.audio_player import AudioPlayer, Esp32AudioPlayer
from pi_app.conversation_session import ConversationSession, MagicResult
from pi_app.decision_client import DecisionClient
from pi_app.esp32_link import EspLink, estimate_audio_seconds, word_durations_ms
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.tts_client import TtsClient, TtsError

RECONNECT_DELAY_S = 3.0
MAGIC_RESULT_PATH = "tools/magic_result.json"
_BUDGET_EXHAUSTED_REPLY = "The mists are resting a moment... try again shortly, traveler."


class TeammateClient:
    def __init__(
        self,
        ws_url: str,
        decision_client: DecisionClient,
        tts_client: TtsClient,
        request_budget: RequestBudget,
        character_budget: CharacterBudget,
        esp32_link: EspLink,
        audio_player: AudioPlayer,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
        magic_result_path: str = MAGIC_RESULT_PATH,
        logger: logging.Logger | None = None,
    ):
        self._ws_url = ws_url
        self._decision_client = decision_client
        self._tts_client = tts_client
        self._request_budget = request_budget
        self._character_budget = character_budget
        self._esp32_link = esp32_link
        self._audio_player = audio_player
        self._reconnect_delay_s = reconnect_delay_s
        self._magic_result_path = magic_result_path
        self._logger = logger or logging.getLogger(__name__)
        self._session: ConversationSession | None = None
        # The guest may talk over Tubi: messages are handled in their own tasks
        # so the socket keeps being read during playback. One turn runs at a
        # time (the lock); anything said while a turn is in flight waits in
        # _pending and is merged into a single next turn.
        self._turn_lock = asyncio.Lock()
        self._pending: list[str] = []
        self._tasks: set[asyncio.Task] = set()

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
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._spawn(self.handle_message(ws, msg.data))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break
            finally:
                for task in list(self._tasks):
                    task.cancel()
                self._pending.clear()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._logger.error("message handler failed", exc_info=task.exception())

    async def handle_message(self, ws, raw: str):
        try:
            data = json.loads(raw)
        except ValueError:
            return

        msg_type = data.get("type")
        payload = data.get("payload") or {}

        if msg_type == "session:start":
            session_id = payload.get("sessionId") or str(uuid.uuid4())
            self._logger.info(f"session:start {session_id}")
            self._pending.clear()
            async with self._turn_lock:
                self._session = ConversationSession(session_id, self._decision_client)
                reply = await self._run_llm(self._session.start)
                await self._speak_turn(ws, reply)
            return

        if msg_type == "session:end":
            self._logger.info("session:end")
            self._pending.clear()
            self._session = None
            return

        if msg_type != "transcript":
            return

        if self._session is None:
            self._logger.warning("transcript received with no active session; ignoring")
            return

        text = payload.get("text", "")
        if not text or not text.strip():
            return

        self._pending.append(text.strip())
        async with self._turn_lock:
            if not self._pending:
                return  # an earlier turn already merged this one in
            text = " ".join(self._pending)
            self._pending.clear()

            session = self._session
            if session is None:  # session ended while this waited its turn
                return

            self._logger.info(f"guest said: {text!r}")
            reply = await self._run_llm(lambda: session.respond(text))
            await self._speak_turn(ws, reply)

            if session.done:
                await self._reveal(ws, session.result)
                self._session = None

    async def _run_llm(self, fn) -> str:
        if not self._request_budget.try_consume():
            self._logger.warning(
                f"decision request budget exhausted "
                f"(daily={self._request_budget.daily_count}/{self._request_budget.daily_limit}, "
                f"total={self._request_budget.total_count}/{self._request_budget.total_limit})"
            )
            return _BUDGET_EXHAUSTED_REPLY

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    async def _speak_turn(self, ws, text: str):
        self._logger.info(f"Tubi says: {text!r}")
        await ws.send_json({"type": "speak", "payload": {"text": text}})

        loop = asyncio.get_running_loop()
        audio = None
        if self._character_budget.try_consume(len(text)):
            try:
                audio = await loop.run_in_executor(None, self._tts_client.synthesize, text)
            except TtsError as e:
                self._logger.warning(f"TTS call failed, sending text only: {e}")
        else:
            self._logger.warning("TTS character budget exhausted, sending text only")

        if audio:
            # The ESP32 runs the head/arm animation on its own from the
            # per-word schedule, so this just kicks it off alongside playback.
            word_ms = word_durations_ms(
                text, estimate_audio_seconds(audio, self._tts_client.bytes_per_second)
            )
            await loop.run_in_executor(None, self._esp32_link.talk, word_ms)
            await loop.run_in_executor(None, self._audio_player.play, audio)

    async def _reveal(self, ws, result: MagicResult):
        await ws.send_json({
            "type": "display",
            "payload": {
                "kind": "magic-assessment",
                "magic_type": result.magic_type,
                "magic_level": result.magic_level,
            },
        })

        try:
            with open(self._magic_result_path, "w") as f:
                json.dump(dataclasses.asdict(result), f, indent=2)
        except OSError as e:
            self._logger.warning(f"could not write magic result to {self._magic_result_path}: {e}")


def main():
    import os

    logging.basicConfig(level=logging.INFO)

    from pi_app.dotenv import load_secrets_env
    from pi_app.hardware import open_hardware

    load_secrets_env() # load the secrets

    decision_client = DecisionClient( # cloud based llm we are using for reasoning
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-5-nano"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    audio_port = os.environ.get("AUDIO_SERIAL_PORT")
    tts_client = TtsClient( # cloud based llm we are using for text to speech
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
        # The ESP32 audio firmware only plays 22.05 kHz mp3.
        output_format=os.environ.get("TTS_OUTPUT_FORMAT") or ("mp3_22050_32" if audio_port else None),
    )

    # Speech and motion hardware from the environment (one-board shared wire, separate links, or the
    # Pi's own speaker); never raises -- anything unopenable degrades with a logged error.
    esp32_link, audio_player = open_hardware(os.environ, tts_client.bytes_per_second)

    client = TeammateClient( # this is the conversational part
        ws_url=os.environ.get("WEBSITE_WS_URL", "ws://localhost:3000/?role=teammate"),
        decision_client=decision_client,
        tts_client=tts_client,
        request_budget=RequestBudget(daily_limit=int(os.environ.get("LLM_DAILY_REQUEST_CAP", "300"))),
        character_budget=CharacterBudget(daily_limit=int(os.environ.get("TTS_DAILY_CHAR_CAP", "20000"))),
        esp32_link=esp32_link,
        audio_player=audio_player,
    )
    asyncio.run(client.run_forever())


if __name__ == "__main__":
    main()
