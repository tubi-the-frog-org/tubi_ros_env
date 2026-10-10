import json

import pytest

from pi_app.snacks import Line, Snack
from pi_app.decision_client import (
    DecisionInvalidResponseError,
    DecisionClient,
    DecisionRequestError,
    DecisionTimeoutError,
    DecisionValidationError,
    build_conversation_schema,
    build_schema,
    build_system_prompt,
    validate_reply,
)
from fake_decision_server import FakeDecisionServer

COOKIES = Snack(id="cookies", label="Cookies", slot=1,
                 aliases=["biscuit"], tags=["sweet"], lines=[Line(id="cookies_1", text="cookies!")])
CHIPS = Snack(id="chips", label="Chips", slot=3,
              aliases=["crisps"], tags=["salty", "crunchy"], lines=[Line(id="chips_1", text="chips!")])
SNACKS = [COOKIES, CHIPS]


def _client(url, timeout_s=5.0):
    return DecisionClient(base_url=url, model="gpt-5-nano", api_key="sk-test", timeout_s=timeout_s)


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
    with pytest.raises(DecisionValidationError):
        validate_reply("", CHIPS)


def test_validate_reply_rejects_too_long():
    with pytest.raises(DecisionValidationError):
        validate_reply("chips " * 100, CHIPS)


def test_validate_reply_rejects_url():
    with pytest.raises(DecisionValidationError):
        validate_reply("The chips await! visit http://example.com", CHIPS)


def test_validate_reply_rejects_missing_snack_mention():
    with pytest.raises(DecisionValidationError):
        validate_reply("A mystical treat awaits you, traveler!", CHIPS)


def test_validate_reply_accepts_label_or_alias_mention():
    validate_reply("The crisps call to you!", CHIPS)  # alias
    validate_reply("Chips, destiny demands it!", CHIPS)  # label


def test_choose_snack_and_reply_success():
    with FakeDecisionServer(responses=[{"status": 200, "snack": "chips", "reply": "The crunchy chips await!"}]) as server:
        client = _client(server.url)
        snack_id, reply = client.choose_snack_and_reply("something crunchy", SNACKS)
        assert snack_id == "chips"
        assert reply == "The crunchy chips await!"


def test_choose_snack_and_reply_invalid_snack_raises():
    with FakeDecisionServer(responses=[{"status": 200, "snack": "not_a_snack", "reply": "whatever"}]) as server:
        client = _client(server.url)
        with pytest.raises(DecisionInvalidResponseError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_choose_snack_and_reply_bad_json_content_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": "not json"}]) as server:
        client = _client(server.url)
        with pytest.raises(DecisionInvalidResponseError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_choose_snack_and_reply_validation_failure_raises():
    with FakeDecisionServer(responses=[{"status": 200, "snack": "chips", "reply": "a vague mystical thing"}]) as server:
        client = _client(server.url)
        with pytest.raises(DecisionValidationError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_retries_once_on_429_then_succeeds():
    with FakeDecisionServer(responses=[
        {"status": 429},
        {"status": 200, "snack": "chips", "reply": "Chips, as fate decreed!"},
    ]) as server:
        client = _client(server.url)
        snack_id, reply = client.choose_snack_and_reply("hello", SNACKS)
        assert snack_id == "chips"
        assert server.call_count == 2


def test_raises_after_retry_also_fails():
    with FakeDecisionServer(responses=[{"status": 429}, {"status": 429}]) as server:
        client = _client(server.url)
        with pytest.raises(DecisionRequestError):
            client.choose_snack_and_reply("hello", SNACKS)
        assert server.call_count == 2  # exactly one retry, not more


def test_non_retryable_error_status_raises_without_retry():
    with FakeDecisionServer(responses=[{"status": 400}]) as server:
        client = _client(server.url)
        with pytest.raises(DecisionRequestError):
            client.choose_snack_and_reply("hello", SNACKS)
        assert server.call_count == 1


def test_timeout_raises():
    with FakeDecisionServer(responses=[{"status": 200, "snack": "chips", "reply": "chips!"}], delay_s=1.0) as server:
        client = _client(server.url, timeout_s=0.2)
        with pytest.raises(DecisionTimeoutError):
            client.choose_snack_and_reply("hello", SNACKS)


def test_connection_refused_raises():
    client = DecisionClient(base_url="http://127.0.0.1:1", model="gpt-5-nano", api_key="sk-test", timeout_s=2)
    with pytest.raises(DecisionRequestError):
        client.choose_snack_and_reply("hello", SNACKS)


# ---- converse() ----

def _history():
    return [
        {"role": "system", "content": build_system_prompt(SNACKS)},
        {"role": "user", "content": "(session start)"},
    ]


def test_build_conversation_schema_shape():
    schema = build_conversation_schema(["cookies", "chips"])
    assert schema["properties"]["snack"]["enum"] == ["cookies", "chips", "undecided"]
    assert set(schema["properties"]["percentages"]["properties"]) == {"cookies", "chips"}
    assert schema["required"] == ["done", "snack", "reply", "percentages"]


def _not_done_content(reply="Greetings! Tell me about yourself."):
    return json.dumps({"done": False, "snack": "undecided", "reply": reply, "percentages": {"cookies": 0, "chips": 0}})


def _done_content(snack="chips", reply="The crunchy chips await!", percentages=None):
    percentages = percentages if percentages is not None else {"cookies": 20, "chips": 80}
    return json.dumps({"done": True, "snack": snack, "reply": reply, "percentages": percentages})


def test_converse_not_done_returns_reply_without_snack_or_percentages():
    with FakeDecisionServer(responses=[{"status": 200, "content": _not_done_content()}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        turn = client.converse(_history(), SNACKS)
        assert turn.done is False
        assert turn.reply_text == "Greetings! Tell me about yourself."
        assert turn.snack_id is None
        assert turn.percentages is None


def test_converse_done_returns_snack_and_normalized_percentages():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(percentages={"cookies": 20, "chips": 60})}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        turn = client.converse(_history(), SNACKS)
        assert turn.done is True
        assert turn.snack_id == "chips"
        # 20/80 and 60/80 renormalized to sum to 100
        assert turn.percentages == {"cookies": 25.0, "chips": 75.0}


def test_converse_done_with_all_zero_percentages_does_not_divide_by_zero():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(percentages={"cookies": 0, "chips": 0})}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        turn = client.converse(_history(), SNACKS)
        assert turn.percentages == {"cookies": 0.0, "chips": 0.0}


def test_converse_done_invalid_snack_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(snack="not_a_snack")}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        with pytest.raises(DecisionInvalidResponseError):
            client.converse(_history(), SNACKS)


def test_converse_done_reply_not_mentioning_snack_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(reply="A vague mystical thing.")}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        with pytest.raises(DecisionValidationError):
            client.converse(_history(), SNACKS)


def test_converse_not_done_long_reply_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": _not_done_content(reply="x" * 400)}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        with pytest.raises(DecisionValidationError):
            client.converse(_history(), SNACKS)


def test_converse_empty_reply_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": _not_done_content(reply="")}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        with pytest.raises(DecisionValidationError):
            client.converse(_history(), SNACKS)


def test_converse_bad_json_content_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": "not json"}]) as server:
        client = DecisionClient(base_url=server.url, model="gpt-5-nano", api_key="sk-test", timeout_s=5)
        with pytest.raises(DecisionInvalidResponseError):
            client.converse(_history(), SNACKS)
