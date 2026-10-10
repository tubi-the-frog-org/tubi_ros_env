import pytest

from animatronic_common.snacks import Line, Snack
from animatronic_brain.cloud_client import (
    CloudInvalidResponseError,
    CloudLlmClient,
    CloudRequestError,
    CloudTimeoutError,
    CloudValidationError,
    build_schema,
    build_system_prompt,
    validate_reply,
)
from fake_cloud_server import FakeCloudServer

COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=["biscuit"], tags=["sweet"], lines=[Line(id="cookies_1", text="cookies!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, angle=195,
              aliases=["crisps"], tags=["salty", "crunchy"], lines=[Line(id="chips_1", text="chips!")])
SNACKS = [COOKIES, CHIPS]


def _client(url, timeout_s=5.0):
    return CloudLlmClient(base_url=url, model="gpt-5-nano", api_key="sk-test", timeout_s=timeout_s)


def test_build_schema_shape():
    schema = build_schema(["cookies", "chips"])
    assert schema["properties"]["snack"]["enum"] == ["cookies", "chips"]
    assert schema["required"] == ["snack", "reply"]
    assert schema["additionalProperties"] is False


def test_build_system_prompt_includes_tags_and_injection_defense():
    prompt = build_system_prompt(SNACKS)
    assert "chips" in prompt and "crunchy" in prompt
    assert "DATA" in prompt


def test_validate_reply_rejects_empty():
    with pytest.raises(CloudValidationError):
        validate_reply("", CHIPS)


def test_validate_reply_rejects_too_long():
    with pytest.raises(CloudValidationError):
        validate_reply("chips " * 100, CHIPS)


def test_validate_reply_rejects_url():
    with pytest.raises(CloudValidationError):
        validate_reply("The chips await! visit http://example.com", CHIPS)


def test_validate_reply_rejects_missing_snack_mention():
    with pytest.raises(CloudValidationError):
        validate_reply("A mystical treat awaits you, traveler!", CHIPS)


def test_validate_reply_accepts_label_or_alias_mention():
    validate_reply("The crisps call to you!", CHIPS)  # alias
    validate_reply("Chips, destiny demands it!", CHIPS)  # label


def test_choose_snack_and_reply_success():
    with FakeCloudServer(responses=[{"status": 200, "snack": "chips", "reply": "The crunchy chips await!"}]) as server:
        client = _client(server.url)
        snack_id, reply = client.choose_snack_and_reply("something crunchy", SNACKS)
        assert snack_id == "chips"
        assert reply == "The crunchy chips await!"


def test_choose_snack_and_reply_invalid_snack_raises():
    with FakeCloudServer(responses=[{"status": 200, "snack": "not_a_snack", "reply": "whatever"}]) as server:
        client = _client(server.url)
        with pytest.raises(CloudInvalidResponseError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_choose_snack_and_reply_bad_json_content_raises():
    with FakeCloudServer(responses=[{"status": 200, "content": "not json"}]) as server:
        client = _client(server.url)
        with pytest.raises(CloudInvalidResponseError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_choose_snack_and_reply_validation_failure_raises():
    with FakeCloudServer(responses=[{"status": 200, "snack": "chips", "reply": "a vague mystical thing"}]) as server:
        client = _client(server.url)
        with pytest.raises(CloudValidationError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_retries_once_on_429_then_succeeds():
    with FakeCloudServer(responses=[
        {"status": 429},
        {"status": 200, "snack": "chips", "reply": "Chips, as fate decreed!"},
    ]) as server:
        client = _client(server.url)
        snack_id, reply = client.choose_snack_and_reply("hello", SNACKS)
        assert snack_id == "chips"
        assert server.call_count == 2


def test_raises_after_retry_also_fails():
    with FakeCloudServer(responses=[{"status": 429}, {"status": 429}]) as server:
        client = _client(server.url)
        with pytest.raises(CloudRequestError):
            client.choose_snack_and_reply("hello", SNACKS)
        assert server.call_count == 2  # exactly one retry, not more


def test_non_retryable_error_status_raises_without_retry():
    with FakeCloudServer(responses=[{"status": 400}]) as server:
        client = _client(server.url)
        with pytest.raises(CloudRequestError):
            client.choose_snack_and_reply("hello", SNACKS)
        assert server.call_count == 1


def test_timeout_raises():
    with FakeCloudServer(responses=[{"status": 200, "snack": "chips", "reply": "chips!"}], delay_s=1.0) as server:
        client = _client(server.url, timeout_s=0.2)
        with pytest.raises(CloudTimeoutError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_connection_refused_raises():
    client = CloudLlmClient(base_url="http://127.0.0.1:1", model="gpt-5-nano", api_key="sk-test", timeout_s=2)
    with pytest.raises(CloudRequestError):
        client.choose_snack_and_reply("hello", SNACKS)
