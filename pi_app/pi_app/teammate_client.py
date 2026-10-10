"""Connects outbound to the website's WebSocket hub as "the teammate
system" (see website/docs/message-contract.md and website/CLAUDE.md's
"Open question 2"): the website's server broadcasts to every connected
client, so this is just another client on the same socket.

Drives the multi-turn ConversationSession (conversation_session.py):
`session:start` begins one, `transcript` feeds it, `session:end`
clears it. Sends a `speak` message per turn and an extended `display`
message (with percentages) on reveal.

Scope note: `speak` and `display.payload.percentages` are NOT yet in
website/docs/message-contract.md -- that contract only covers the old
one-shot shape. This is a proposal (see
pi_app/docs/multi_turn_contract_proposal.md) for what the website side
needs to add to actually drive a multi-turn UI; until then, sending
these is harmless (the website's broadcast-everyone server will just
pass them through to a browser that doesn't yet know what to do with
them), but it isn't a real integration until that's agreed and built.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid

import aiohttp

from pi_app.audio_player import AudioPlayer
from pi_app.conversation_session import ConversationSession, QuizResult
from pi_app.decision_client import DecisionClient
from pi_app.esp32_link import EspLink, estimate_audio_seconds, word_durations_ms
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.snacks import SnacksStore
from pi_app.tts_client import TtsClient, TtsError

RECONNECT_DELAY_S = 3.0
DISPENSE_OPEN_SECONDS = 4.0
SLOT_SETTLE_SECONDS = 3.0  # time for the lazy susan to reach its bin before the door opens
QUIZ_RESULT_PATH = "tools/quiz_result.json"
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
        snacks_store: SnacksStore,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
        dispense_open_seconds: float = DISPENSE_OPEN_SECONDS,
        slot_settle_seconds: float = SLOT_SETTLE_SECONDS,
        quiz_result_path: str = QUIZ_RESULT_PATH,
        logger: logging.Logger | None = None,
    ):
        self._ws_url = ws_url
        self._decision_client = decision_client
        self._tts_client = tts_client
        self._request_budget = request_budget
        self._character_budget = character_budget
        self._esp32_link = esp32_link
        self._audio_player = audio_player
        self._snacks_store = snacks_store
        self._reconnect_delay_s = reconnect_delay_s
        self._dispense_open_seconds = dispense_open_seconds
        self._slot_settle_seconds = slot_settle_seconds
        self._quiz_result_path = quiz_result_path
        self._logger = logger or logging.getLogger(__name__)
        self._session: ConversationSession | None = None

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
        try:
            data = json.loads(raw)
        except ValueError:
            return

        msg_type = data.get("type")
        payload = data.get("payload") or {}

        if msg_type == "session:start":
            session_id = payload.get("sessionId") or str(uuid.uuid4())
            self._session = ConversationSession(session_id, self._decision_client, self._snacks_store.current)
            reply = await self._run_llm(self._session.start)
            await self._speak_turn(ws, reply)
            return

        if msg_type == "session:end":
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

        session = self._session
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
        await ws.send_json({"type": "speak", "payload": {"text": text}})
        try:
            await self._synthesize_and_play(ws, text)
        finally:
            # Always tell the website the turn is over (even with no audio:
            # budget exhausted, TTS failure) -- it keeps the guest's talk
            # button disabled until it hears this.
            await ws.send_json({"type": "state", "payload": {"speaking": False}})

    async def _synthesize_and_play(self, ws, text: str):
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
            await ws.send_json({"type": "state", "payload": {"speaking": True}})
            # The ESP32 runs the head/arm animation on its own from the
            # per-word schedule, so this just kicks it off alongside playback.
            word_ms = word_durations_ms(text, estimate_audio_seconds(audio))
            await loop.run_in_executor(None, self._esp32_link.talk, word_ms)
            await loop.run_in_executor(None, self._audio_player.play, audio)

    async def _reveal(self, ws, result: QuizResult):
        await ws.send_json({
            "type": "display",
            "payload": {
                "kind": "result",
                "snacks": [s.label for s in result.chosen_snacks],
                "percentages": result.percentages,
            },
        })

        # Only one physical mechanism exists right now (one trap door, no
        # per-slot routing -- see esp32_link.py), so every reveal just
        # pulses it open then closed once, regardless of how many snacks
        # were chosen digitally (the firmware can't dispense more than one).
        loop = asyncio.get_running_loop()
        slot = result.chosen_snacks[0].slot
        if 1 <= slot <= 4:
            await loop.run_in_executor(None, self._esp32_link.set_slot, slot)
            await asyncio.sleep(self._slot_settle_seconds)
        await loop.run_in_executor(None, self._esp32_link.open_trap_door)
        await asyncio.sleep(self._dispense_open_seconds)
        await loop.run_in_executor(None, self._esp32_link.close_trap_door)

        try:
            with open(self._quiz_result_path, "w") as f:
                json.dump(dataclasses.asdict(result), f, indent=2)
        except OSError as e:
            self._logger.warning(f"could not write quiz result to {self._quiz_result_path}: {e}")


def main():
    import os

    logging.basicConfig(level=logging.INFO)

    from pi_app.dotenv import load_secrets_env
    from pi_app.esp32_link import NullEspLink, SerialEspLink

    load_secrets_env() # load the secrets

    snacks_path = os.environ.get("SNACKS_CONFIG_PATH", "config/snacks.json") # the place where snacks are stored
    snacks_store = SnacksStore(snacks_path)
 
    decision_client = DecisionClient( # cloud based llm we are using for reasoning
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-5-nano"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    tts_client = TtsClient( # cloud based llm we are using for text to speech
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
    )

    esp32_port = os.environ.get("ESP32_SERIAL_PORT") # opening the port
    esp32_link = SerialEspLink(esp32_port) if esp32_port else NullEspLink() # link the serial port of esp

    client = TeammateClient( # this is the conversational part
        ws_url=os.environ.get("WEBSITE_WS_URL", "ws://localhost:3000/?role=teammate"),
        decision_client=decision_client,
        tts_client=tts_client,
        request_budget=RequestBudget(daily_limit=int(os.environ.get("LLM_DAILY_REQUEST_CAP", "300"))),
        character_budget=CharacterBudget(daily_limit=int(os.environ.get("TTS_DAILY_CHAR_CAP", "20000"))),
        esp32_link=esp32_link,
        audio_player=AudioPlayer(),
        snacks_store=snacks_store,
    )
    asyncio.run(client.run_forever())


if __name__ == "__main__":
    main()
