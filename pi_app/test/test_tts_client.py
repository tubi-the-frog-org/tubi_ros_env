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
        assert server.call_count == 2  # a 5xx is retried once


def test_client_error_is_not_retried():
    with FakeTtsServer(status_code=401) as server:
        client = _client(server.url)
        with pytest.raises(TtsRequestError):
            client.synthesize("hello")
        assert server.call_count == 1  # a bad key won't fix itself


def test_a_hung_first_request_is_retried_and_succeeds():
    """The real failure: the first request after an idle spell hangs on a dead pooled connection."""
    with FakeTtsServer(audio_bytes=b"recovered", delay_s=1.0, slow_first_calls=1) as server:
        client = _client(server.url, timeout_s=0.3)
        assert client.synthesize("hello") == b"recovered"
        assert server.call_count == 2


def test_every_request_asks_for_a_fresh_connection():
    with FakeTtsServer() as server:
        _client(server.url).synthesize("hello")
        assert server.last_headers.get("connection", "").lower() == "close"


def test_timeout_raises_after_one_retry():
    with FakeTtsServer(delay_s=1.0) as server:
        client = _client(server.url, timeout_s=0.2)
        with pytest.raises(TtsTimeoutError):
            client.synthesize("hello")
        assert server.call_count == 2


def test_connection_refused_raises():
    client = TtsClient(base_url="http://127.0.0.1:1", model="eleven_turbo_v2_5", api_key="sk-test", timeout_s=2)
    with pytest.raises(TtsRequestError):
        client.synthesize("hello")
