import asyncio
import json

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from pi_app.decision_client import ConversationTurn
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.teammate_client import TeammateClient


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def converse(self, messages):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn


class StubTtsClient:
    bytes_per_second = 16000

    def __init__(self, result=b"mp3-bytes"):
        self.result = result
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return self.result


class RecordingEspLink:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.slots = []
        self.talks = []
        self.dispensed = []

    def dispense(self, slot):
        self.dispensed.append(slot)

    def set_slot(self, slot):
        self.slots.append(slot)

    def talk(self, word_ms):
        self.talks.append(word_ms)

    def open_trap_door(self):
        self.opened += 1

    def close_trap_door(self):
        self.closed += 1


class RecordingAudioPlayer:
    def __init__(self):
        self.played = []

    def play(self, audio_mp3):
        self.played.append(audio_mp3)


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


def _not_done(reply="Tell me about yourself!"):
    return ConversationTurn(reply_text=reply, done=False, magic_type=None, magic_level=None)


def _done(magic_type="Stormweaver", magic_level=7, reply="Your stormweaver spark is level seven!"):
    return ConversationTurn(
        reply_text=reply,
        done=True,
        magic_type=magic_type,
        magic_level=magic_level,
    )


def _client(turns, result_path, esp32_link=None, audio_player=None, request_budget=None, character_budget=None):
    decision_client = StubDecisionClient(turns)
    client = TeammateClient(
        "ws://unused",
        decision_client,
        StubTtsClient(),
        request_budget or RequestBudget(),
        character_budget or CharacterBudget(),
        esp32_link or RecordingEspLink(),
        audio_player or RecordingAudioPlayer(),
        magic_result_path=str(result_path),
    )
    return client, decision_client


async def test_session_start_begins_a_conversation_and_speaks_intro(tmp_path):
    client, decision_client = _client([_not_done("Greetings, traveler!")], tmp_path / "result.json")
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {"sessionId": "s1"}}))

    assert decision_client.calls == 1
    assert {"type": "speak", "payload": {"text": "Greetings, traveler!"}} in ws.sent
    assert client._session is not None
    assert client._session.session_id == "s1"


async def test_transcript_with_no_active_session_is_ignored(tmp_path):
    client, decision_client = _client([_not_done()], tmp_path / "result.json")
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "hello"}}))

    assert decision_client.calls == 0
    assert ws.sent == []


async def test_transcript_advances_session_without_revealing(tmp_path):
    client, decision_client = _client([_not_done("Hi!"), _not_done("What's your vibe?")], tmp_path / "result.json")
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "I'm Max"}}))

    assert decision_client.calls == 2
    assert {"type": "speak", "payload": {"text": "What's your vibe?"}} in ws.sent
    assert not any(m["type"] == "display" for m in ws.sent)
    assert client._session is not None  # still ongoing


async def test_reveal_sends_magic_assessment_and_plays_audio_without_dispensing(tmp_path):
    result_path = tmp_path / "result.json"
    client, decision_client = _client(
        [_not_done("Hi!"), _done()],
        result_path,
    )
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "yes reveal it"}}))

    display_msgs = [m for m in ws.sent if m["type"] == "display"]
    assert display_msgs == [{"type": "display", "payload": {
        "kind": "magic-assessment", "magic_type": "Stormweaver", "magic_level": 7,
    }}]
    assert client._esp32_link.dispensed == []
    assert client._audio_player.played == [b"mp3-bytes", b"mp3-bytes"]  # intro + reveal
    assert client._session is None  # cleared after reveal
    saved = json.loads(result_path.read_text())
    assert saved["magic_type"] == "Stormweaver"
    assert saved["magic_level"] == 7


async def test_session_end_clears_session(tmp_path):
    client, _ = _client([_not_done("Hi!")], tmp_path / "result.json")
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))
    assert client._session is not None

    await client.handle_message(ws, json.dumps({"type": "session:end"}))

    assert client._session is None


async def test_request_budget_exhausted_sends_fallback_without_calling_llm(tmp_path):
    client, decision_client = _client(
        [_not_done("Hi!")], tmp_path / "result.json",
        request_budget=RequestBudget(daily_limit=0),
    )
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    assert decision_client.calls == 0
    assert any("mists are resting" in m["payload"]["text"] for m in ws.sent if m["type"] == "speak")


async def test_character_budget_exhausted_skips_audio(tmp_path):
    audio_player = RecordingAudioPlayer()
    client, _ = _client(
        [_not_done("Hi!")], tmp_path / "result.json",
        audio_player=audio_player,
        character_budget=CharacterBudget(daily_limit=0),
    )
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    assert audio_player.played == []
    assert not any(m["type"] == "state" for m in ws.sent)  # no tubi/speaking state anymore


async def test_real_websocket_roundtrip(tmp_path):
    """End-to-end sanity check against a real (test) websocket server."""
    received_from_client = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session:start", "payload": {"sessionId": "s1"}})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                received_from_client.append(json.loads(msg.data))
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/", ws_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        client, _ = _client([_not_done("Greetings!")], tmp_path / "result.json")
        client._ws_url = str(server.make_url("/")).replace("http://", "ws://")

        async with aiohttp.ClientSession() as session:
            await asyncio.wait_for(client._connect_and_listen(session), timeout=5)

        assert received_from_client[0] == {"type": "speak", "payload": {"text": "Greetings!"}}
    finally:
        await server.close()


class SlowAudioPlayer:
    """Playback that takes a moment, so the guest can talk over it."""

    def __init__(self):
        self.played = []

    def play(self, audio_mp3):
        import time
        self.played.append(audio_mp3)
        time.sleep(0.3)


async def test_guest_talking_over_tubi_is_heard_and_merged_into_one_turn(tmp_path):
    client, decision_client = _client(
        [_not_done("Intro."), _not_done("First reply."), _not_done("Merged reply.")],
        tmp_path / "result.json", audio_player=SlowAudioPlayer(),
    )
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {"sessionId": "s1"}}))
    seen = []
    real_converse = decision_client.converse

    def spy(messages):
        seen.append(messages[-1]["content"])
        return real_converse(messages)

    decision_client.converse = spy

    # First answer starts a turn whose playback is slow; two more arrive while Tubi is still talking.
    send = lambda text: client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": text}}))
    first = asyncio.create_task(send("My name is Dave"))
    await asyncio.sleep(0.15)
    second = asyncio.create_task(send("and I like"))
    third = asyncio.create_task(send("secret doors"))
    await asyncio.gather(first, second, third)

    assert seen == ["My name is Dave", "and I like secret doors"]  # two turns, not three
    assert [m["payload"]["text"] for m in ws.sent if m["type"] == "speak"] == ["Intro.", "First reply.", "Merged reply."]
    assert not any(m["type"] == "state" for m in ws.sent)
