import json

import pytest

from pi_app.esp32_link import EspLink
from pi_app.message_package import SOURCE_OK, SnackMessage
from pi_app.server import create_app
from pi_app.snacks import SnacksStore


class StubDecisionService:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def handle(self, request_id, raw_text, snacks):
        self.calls.append((request_id, raw_text))
        return self.message


class RecordingEspLink(EspLink):
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def _write_snacks(tmp_path):
    path = tmp_path / "snacks.json"
    path.write_text(json.dumps({
        "version": 1,
        "default_snack": "none",
        "snacks": [
            {"id": "none", "label": "No snack", "slot": 0, "angle": 0,
             "aliases": [], "tags": [], "lines": [{"id": "none_1", "text": "nothing."}]},
        ],
    }))
    return path


@pytest.fixture
def snacks_store(tmp_path):
    return SnacksStore(str(_write_snacks(tmp_path)))


async def test_speak_requires_bearer_token_when_configured(aiohttp_client, snacks_store):
    service = StubDecisionService(SnackMessage("r1", "none", "hi", None, SOURCE_OK))
    app = create_app(snacks_store, service, RecordingEspLink(), web_bridge_token="secret")
    client = await aiohttp_client(app)

    resp = await client.post("/speak", json={"text": "hello"})
    assert resp.status == 401


async def test_speak_succeeds_with_correct_token(aiohttp_client, snacks_store):
    service = StubDecisionService(SnackMessage("r1", "none", "hi there", None, SOURCE_OK))
    esp32_link = RecordingEspLink()
    app = create_app(snacks_store, service, esp32_link, web_bridge_token="secret")
    client = await aiohttp_client(app)

    resp = await client.post("/speak", json={"text": "hello"}, headers={"Authorization": "Bearer secret"})
    assert resp.status == 200
    body = await resp.json()
    assert body["snack_id"] == "none"
    assert body["text"] == "hi there"
    assert body["audio_available"] is False
    assert len(esp32_link.sent) == 1
    assert esp32_link.sent[0].snack_id == "none"


async def test_speak_with_no_token_configured_skips_auth(aiohttp_client, snacks_store):
    service = StubDecisionService(SnackMessage("r1", "none", "hi", None, SOURCE_OK))
    app = create_app(snacks_store, service, RecordingEspLink(), web_bridge_token="")
    client = await aiohttp_client(app)

    resp = await client.post("/speak", json={"text": "hello"})
    assert resp.status == 200


async def test_speak_rejects_missing_text(aiohttp_client, snacks_store):
    app = create_app(snacks_store, StubDecisionService(None), RecordingEspLink(), web_bridge_token="")
    client = await aiohttp_client(app)

    resp = await client.post("/speak", json={})
    assert resp.status == 400


async def test_speak_rejects_text_too_long(aiohttp_client, snacks_store):
    app = create_app(snacks_store, StubDecisionService(None), RecordingEspLink(), web_bridge_token="")
    client = await aiohttp_client(app)

    resp = await client.post("/speak", json={"text": "x" * 301})
    assert resp.status == 400


async def test_healthz(aiohttp_client, snacks_store):
    app = create_app(snacks_store, StubDecisionService(None), RecordingEspLink(), web_bridge_token="")
    client = await aiohttp_client(app)

    resp = await client.get("/healthz")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
