import asyncio
import json

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from pi_app.message_package import SOURCE_OK, SOURCE_OK_NO_AUDIO, SnackMessage
from pi_app.snacks import SnacksStore
from pi_app.teammate_client import TeammateClient


class StubDecisionService:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def handle(self, request_id, raw_text, snacks):
        self.calls.append((request_id, raw_text))
        return self.message


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


def _client(snacks_store, message=None, esp32_link=None, audio_player=None):
    service = StubDecisionService(message)
    client = TeammateClient(
        "ws://unused",
        service,
        esp32_link or RecordingEspLink(),
        audio_player or RecordingAudioPlayer(),
        snacks_store,
        dispense_open_seconds=0,
    )
    return client, service


async def test_ignores_non_transcript_message_types(snacks_store):
    client, service = _client(snacks_store)
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "session-state", "payload": {}}))
    assert service.calls == []
    assert ws.sent == []


async def test_ignores_malformed_json(snacks_store):
    client, service = _client(snacks_store)
    await client.handle_message(object(), "not json")  # must not raise even with a bogus ws
    assert service.calls == []


async def test_ignores_blank_transcript_text(snacks_store):
    client, service = _client(snacks_store)
    ws = FakeWs()
    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "   "}}))
    assert service.calls == []
    assert ws.sent == []


async def test_full_flow_with_audio_opens_and_closes_trap_door(snacks_store):
    message = SnackMessage("req-1", "chips", 3, "Chips await!", b"mp3-bytes", SOURCE_OK)
    esp32_link = RecordingEspLink()
    audio_player = RecordingAudioPlayer()
    client, service = _client(snacks_store, message, esp32_link, audio_player)
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "something crunchy"}}))

    assert service.calls[0][1] == "something crunchy"
    assert {"type": "display", "payload": {"kind": "result", "snack": "Chips"}} in ws.sent
    assert {"type": "state", "payload": {"speaking": True}} in ws.sent
    assert {"type": "state", "payload": {"speaking": False}} in ws.sent
    assert audio_player.played == [b"mp3-bytes"]
    assert esp32_link.opened == 1
    assert esp32_link.closed == 1


async def test_no_audio_path_skips_speaking_state_but_still_dispenses(snacks_store):
    message = SnackMessage("req-2", "none", 0, "Nothing for you.", None, SOURCE_OK_NO_AUDIO)
    esp32_link = RecordingEspLink()
    audio_player = RecordingAudioPlayer()
    client, service = _client(snacks_store, message, esp32_link, audio_player)
    ws = FakeWs()

    await client.handle_message(ws, json.dumps({"type": "transcript", "payload": {"text": "whatever"}}))

    assert {"type": "state", "payload": {"speaking": True}} not in ws.sent
    assert audio_player.played == []
    assert esp32_link.opened == 1
    assert esp32_link.closed == 1


async def test_real_websocket_roundtrip(snacks_store):
    """End-to-end sanity check against a real (test) websocket server,
    to catch aiohttp usage mistakes that unit-testing handle_message
    alone wouldn't catch."""
    received_from_client = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "transcript", "payload": {"text": "something crunchy"}})
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
        message = SnackMessage("req-3", "chips", 3, "Chips!", None, SOURCE_OK_NO_AUDIO)
        service = StubDecisionService(message)
        ws_url = str(server.make_url("/")).replace("http://", "ws://")
        client = TeammateClient(ws_url, service, RecordingEspLink(), RecordingAudioPlayer(), snacks_store, dispense_open_seconds=0)

        async with aiohttp.ClientSession() as session:
            await asyncio.wait_for(client._connect_and_listen(session), timeout=5)

        assert received_from_client[0] == {"type": "display", "payload": {"kind": "result", "snack": "Chips"}}
    finally:
        await server.close()
