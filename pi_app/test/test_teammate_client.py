import asyncio
import json

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from pi_app.decision_client import ConversationTurn
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.snacks import SnacksStore
from pi_app.teammate_client import TeammateClient


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def converse(self, messages, snacks, force_done=False):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn


class StubTtsClient:
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
    return ConversationTurn(reply_text=reply, done=False, snack_id=None, percentages=None)


def _done(snack_id="chips", reply="Chips await!", percentages=None):
    return ConversationTurn(reply_text=reply, done=True, snack_id=snack_id, percentages=percentages or {"cookies": 25.0, "chips": 75.0})


def _write_snacks(tmp_path):
    path = tmp_path / "snacks.json"
    path.write_text(json.dumps({
        "version": 1,
        "default_snack": "none",
        "snacks": [
            {"id": "chips", "label": "Chips", "slot": 3, "aliases": [], "tags": [],
             "lines": [{"id": "c1", "text": "chips!"}]},
            {"id": "none", "label": "No snack", "slot": 0, "aliases": [], "tags": [],
             "lines": [{"id": "n1", "text": "nothing."}]},
        ],
    }))
    return path


@pytest.fixture
def snacks_store(tmp_path):
    return SnacksStore(str(_write_snacks(tmp_path)))


def _client(snacks_store, turns, result_path, esp32_link=None, audio_player=None, request_budget=None, character_budget=None):
    decision_client = StubDecisionClient(turns)
    client = TeammateClient(
        "ws://unused",
        decision_client,
        StubTtsClient(),
        request_budget or RequestBudget(),
        character_budget or CharacterBudget(),
        esp32_link or RecordingEspLink(),
        audio_player or RecordingAudioPlayer(),
        snacks_store,
        dispense_open_seconds=0,
        quiz_result_path=str(result_path),
    )
    return client, decision_client


async def test_session_start_begins_a_conversation_and_speaks_intro(snacks_store, tmp_path):
    client, decision_client = _client(snacks_store, [_not_done("Greetings, traveler!")], tmp_path / "result.json")
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {"sessionId": "s1"}}))

    assert decision_client.calls == 1
    assert {"type": "speak", "payload": {"text": "Greetings, traveler!"}} in ws.sent
    assert client._session is not None
    assert client._session.session_id == "s1"


async def test_transcript_with_no_active_session_is_ignored(snacks_store, tmp_path):
    client, decision_client = _client(snacks_store, [_not_done()], tmp_path / "result.json")
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "hello"}}))

    assert decision_client.calls == 0
    assert ws.sent == []


async def test_transcript_advances_session_without_revealing(snacks_store, tmp_path):
    client, decision_client = _client(snacks_store, [_not_done("Hi!"), _not_done("What's your vibe?")], tmp_path / "result.json")
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "I'm Max"}}))

    assert decision_client.calls == 2
    assert {"type": "speak", "payload": {"text": "What's your vibe?"}} in ws.sent
    assert not any(m["type"] == "display" for m in ws.sent)
    assert client._session is not None  # still ongoing


async def test_reveal_sends_display_with_percentages_plays_audio_and_dispenses(snacks_store, tmp_path):
    result_path = tmp_path / "result.json"
    client, decision_client = _client(
        snacks_store,
        [_not_done("Hi!"), _done(snack_id="chips", reply="Chips await!", percentages={"chips": 80.0, "none": 20.0})],
        result_path,
    )
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "yes reveal it"}}))

    display_msgs = [m for m in ws.sent if m["type"] == "display"]
    assert display_msgs == [{"type": "display", "payload": {"kind": "result", "snack": "Chips", "percentages": {"chips": 80.0, "none": 20.0}}}]
    assert client._esp32_link.opened == 1
    assert client._esp32_link.closed == 1
    assert client._audio_player.played == [b"mp3-bytes", b"mp3-bytes"]  # intro + reveal
    assert client._session is None  # cleared after reveal
    assert json.loads(result_path.read_text())["chosen_snack_id"] == "chips"


async def test_session_end_clears_session(snacks_store, tmp_path):
    client, _ = _client(snacks_store, [_not_done("Hi!")], tmp_path / "result.json")
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))
    assert client._session is not None

    await client.handle_message(ws, json.dumps({"type": "session:end"}))

    assert client._session is None


async def test_request_budget_exhausted_sends_fallback_without_calling_llm(snacks_store, tmp_path):
    client, decision_client = _client(
        snacks_store, [_not_done("Hi!")], tmp_path / "result.json",
        request_budget=RequestBudget(daily_limit=0),
    )
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    assert decision_client.calls == 0
    assert any("mists are resting" in m["payload"]["text"] for m in ws.sent if m["type"] == "speak")


async def test_character_budget_exhausted_skips_audio(snacks_store, tmp_path):
    audio_player = RecordingAudioPlayer()
    client, _ = _client(
        snacks_store, [_not_done("Hi!")], tmp_path / "result.json",
        audio_player=audio_player,
        character_budget=CharacterBudget(daily_limit=0),
    )
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "session:start", "payload": {}}))

    assert audio_player.played == []
    assert not any(m["type"] == "state" for m in ws.sent)


async def test_real_websocket_roundtrip(snacks_store, tmp_path):
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
        client, _ = _client(snacks_store, [_not_done("Greetings!")], tmp_path / "result.json")
        client._ws_url = str(server.make_url("/")).replace("http://", "ws://")

        async with aiohttp.ClientSession() as session:
            await asyncio.wait_for(client._connect_and_listen(session), timeout=5)

        assert received_from_client[0] == {"type": "speak", "payload": {"text": "Greetings!"}}
    finally:
        await server.close()
