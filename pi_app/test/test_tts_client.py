import json

import pytest

from pi_app.tts_client import TtsClient, TtsRequestError, TtsTimeoutError
from fake_tts_server import FakeTtsServer


def _client(url, timeout_s=5.0, voice_id="test-voice"):
    return TtsClient(base_url=url, model="eleven_turbo_v2_5", api_key="sk-test", voice_id=voice_id, timeout_s=timeout_s)


def test_synthesize_returns_audio_bytes():
    with FakeTtsServer(audio_bytes=b"audio-data-here") as server:
        client = _client(server.url)
        audio = client.synthesize("The chips await!")
        assert audio == b"audio-data-here"


def test_synthesize_posts_to_voice_id_path():
    with FakeTtsServer() as server:
        client = _client(server.url, voice_id="my-custom-voice")
        client.synthesize("hello wizard")
        assert server.last_path == "/text-to-speech/my-custom-voice"


def test_synthesize_sends_api_key_header_not_bearer():
    with FakeTtsServer() as server:
        client = _client(server.url)
        client.synthesize("hello")
        assert server.last_headers.get("xi-api-key") == "sk-test"


def test_synthesize_sends_expected_payload_shape():
    with FakeTtsServer() as server:
        client = _client(server.url)
        client.synthesize("hello wizard")
        payload = json.loads(server.last_payload)
        assert payload["text"] == "hello wizard"
        assert payload["model_id"] == "eleven_turbo_v2_5"
        assert "stability" in payload["voice_settings"]
        assert "similarity_boost" in payload["voice_settings"]


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
    client = TtsClient(base_url="http://127.0.0.1:1", model="eleven_turbo_v2_5", api_key="sk-test", timeout_s=2)
    with pytest.raises(TtsRequestError):
        client.synthesize("hello")
