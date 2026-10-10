import json

import pytest

from pi_app.tts_client import TtsClient, TtsRequestError, TtsTimeoutError
from fake_tts_server import FakeTtsServer


def _client(url, timeout_s=5.0):
    return TtsClient(base_url=url, model="gpt-4o-mini-tts", api_key="sk-test", timeout_s=timeout_s)


def test_synthesize_returns_audio_bytes():
    with FakeTtsServer(audio_bytes=b"audio-data-here") as server:
        client = _client(server.url)
        audio = client.synthesize("The chips await!")
        assert audio == b"audio-data-here"


def test_synthesize_sends_expected_payload_shape():
    with FakeTtsServer() as server:
        client = _client(server.url)
        client.synthesize("hello wizard")
        payload = json.loads(server.last_payload)
        assert payload["model"] == "gpt-4o-mini-tts"
        assert payload["input"] == "hello wizard"
        assert payload["response_format"] == "mp3"
        assert "voice" in payload
        assert "instructions" in payload  # default mystical-wizard steering is on by default


def test_synthesize_omits_instructions_when_none():
    with FakeTtsServer() as server:
        client = TtsClient(base_url=server.url, model="gpt-4o-mini-tts", api_key="sk-test", instructions=None)
        client.synthesize("hello")
        payload = json.loads(server.last_payload)
        assert "instructions" not in payload


def test_non_200_raises_request_error():
    with FakeTtsServer(status_code=500) as server:
        client = _client(server.url)
        with pytest.raises(TtsRequestError):
            client.synthesize("hello")


def test_timeout_raises():
    with FakeTtsServer(delay_s=1.0) as server:
        client = _client(server.url, timeout_s=0.2)
        with pytest.raises(TtsTimeoutError):
            client.synthesize("hello")


def test_connection_refused_raises():
    client = TtsClient(base_url="http://127.0.0.1:1", model="gpt-4o-mini-tts", api_key="sk-test", timeout_s=2)
    with pytest.raises(TtsRequestError):
        client.synthesize("hello")
