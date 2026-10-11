import json

import pytest

from pi_app.decision_client import (
    DecisionClient,
    DecisionInvalidResponseError,
    DecisionRequestError,
    DecisionTimeoutError,
    DecisionValidationError,
    build_conversation_schema,
    build_schema,
    build_system_prompt,
    validate_reply,
)
from fake_decision_server import FakeDecisionServer


def _client(url, timeout_s=5.0):
    return DecisionClient(base_url=url, model="gpt-5-nano", api_key="sk-test", timeout_s=timeout_s)


def test_build_schema_shape():
    schema = build_schema()
    assert schema["properties"]["magic_type"] == {"type": "string"}
    assert schema["properties"]["magic_level"] == {"type": "integer"}
    assert schema["required"] == ["magic_type", "magic_level", "reply"]
    assert schema["additionalProperties"] is False


def test_build_system_prompt_keeps_personality_questions_and_magic_assessment():
    prompt = build_system_prompt()
    assert "PERSONALITY" in prompt
    assert "QUESTIONS" in prompt
    assert "magic user" in prompt
    assert "even at random" in prompt
    assert "DATA" in prompt
    assert "snack" not in prompt.lower()


def test_validate_reply_rejects_empty():
    with pytest.raises(DecisionValidationError):
        validate_reply("")


def test_validate_reply_rejects_too_long():
    with pytest.raises(DecisionValidationError):
        validate_reply("word " * 100)


def test_validate_reply_rejects_url():
    with pytest.raises(DecisionValidationError):
        validate_reply("Your magic is unusual! visit http://example.com")


def test_assess_magic_user_returns_type_level_and_reply():
    with FakeDecisionServer(responses=[{
        "status": 200,
        "magic_type": "Stormweaver",
        "magic_level": 7,
        "reply": "You have the instincts of a Stormweaver, level seven.",
    }]) as server:
        result = _client(server.url).assess_magic_user("I like thunderstorms")
    assert result == ("Stormweaver", 7, "You have the instincts of a Stormweaver, level seven.")


@pytest.mark.parametrize("magic_level", [0, 11, -1, True, "7"])
def test_assess_magic_user_rejects_invalid_level(magic_level):
    with FakeDecisionServer(responses=[{
        "status": 200,
        "magic_type": "Stormweaver",
        "magic_level": magic_level,
        "reply": "A magic-user reading.",
    }]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).assess_magic_user("hello")


def test_assess_magic_user_rejects_empty_type():
    with FakeDecisionServer(responses=[{
        "status": 200,
        "magic_type": " ",
        "magic_level": 5,
        "reply": "A magic-user reading.",
    }]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).assess_magic_user("hello")


def test_assess_magic_user_rejects_bad_json_content():
    with FakeDecisionServer(responses=[{"status": 200, "content": "not json"}]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).assess_magic_user("hello")


def test_assess_magic_user_rejects_invalid_reply():
    with FakeDecisionServer(responses=[{
        "status": 200,
        "magic_type": "Stormweaver",
        "magic_level": 5,
        "reply": "",
    }]) as server:
        with pytest.raises(DecisionValidationError):
            _client(server.url).assess_magic_user("hello")


def test_retries_once_on_429_then_succeeds():
    with FakeDecisionServer(responses=[
        {"status": 429},
        {"status": 200, "magic_type": "Stormweaver", "magic_level": 7, "reply": "Stormweaver, clearly."},
    ]) as server:
        result = _client(server.url).assess_magic_user("hello")
        assert result[:2] == ("Stormweaver", 7)
        assert server.call_count == 2


def test_raises_after_retry_also_fails():
    with FakeDecisionServer(responses=[{"status": 429}, {"status": 429}]) as server:
        with pytest.raises(DecisionRequestError):
            _client(server.url).assess_magic_user("hello")
        assert server.call_count == 2


def test_non_retryable_error_status_raises_without_retry():
    with FakeDecisionServer(responses=[{"status": 400}]) as server:
        with pytest.raises(DecisionRequestError):
            _client(server.url).assess_magic_user("hello")
        assert server.call_count == 1


def test_timeout_raises():
    with FakeDecisionServer(responses=[{"status": 200}], delay_s=1.0) as server:
        with pytest.raises(DecisionTimeoutError):
            _client(server.url, timeout_s=0.2).assess_magic_user("hello")


def test_connection_refused_raises():
    client = DecisionClient(base_url="http://127.0.0.1:1", model="gpt-5-nano", api_key="sk-test", timeout_s=2)
    with pytest.raises(DecisionRequestError):
        client.assess_magic_user("hello")


def _history():
    return [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": "(session start)"},
    ]


def _not_done_content(reply="Greetings! Tell me about yourself.", magic_type="", magic_level=0):
    return json.dumps({
        "reasoning": "Curious and careful so far.",
        "done": False,
        "magic_type": magic_type,
        "magic_level": magic_level,
        "reply": reply,
    })


def _done_content(magic_type="Stormweaver", magic_level=7, reply="Your stormweaver spark is level seven!"):
    return json.dumps({
        "reasoning": "The guest is drawn to storms and bold choices.",
        "done": True,
        "magic_type": magic_type,
        "magic_level": magic_level,
        "reply": reply,
    })


def test_build_conversation_schema_shape():
    schema = build_conversation_schema()
    assert schema["properties"]["magic_type"] == {"type": "string"}
    assert schema["properties"]["magic_level"] == {"type": "integer"}
    assert schema["properties"]["done"] == {"type": "boolean"}
    assert schema["required"] == ["reasoning", "done", "magic_type", "magic_level", "reply"]


def test_converse_not_done_has_no_early_assessment():
    with FakeDecisionServer(responses=[{"status": 200, "content": _not_done_content()}]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done is False
    assert turn.reply_text == "Greetings! Tell me about yourself."
    assert turn.magic_type is None
    assert turn.magic_level is None


def test_converse_rejects_early_assessment():
    with FakeDecisionServer(responses=[{
        "status": 200,
        "content": _not_done_content(magic_type="Stormweaver", magic_level=7),
    }]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).converse(_history())


def test_converse_done_returns_magic_result():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content()}]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done is True
    assert turn.magic_type == "Stormweaver"
    assert turn.magic_level == 7
    assert "stormweaver" in turn.reply_text.lower()


def test_converse_done_rejects_invalid_level():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(magic_level=11)}]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).converse(_history())


def test_converse_done_rejects_invalid_reply():
    with FakeDecisionServer(responses=[{"status": 200, "content": _done_content(reply="")}]) as server:
        with pytest.raises(DecisionValidationError):
            _client(server.url).converse(_history())


def test_converse_not_done_overlong_reply_is_truncated():
    with FakeDecisionServer(responses=[{
        "status": 200,
        "content": _not_done_content(reply="word " * 200),
    }]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done is False
    assert 0 < len(turn.reply_text) <= 500


def test_converse_not_done_truncates_at_sentence_boundary_when_possible():
    long_reply = ("Greetings, traveler! " * 20) + ("Extra padding with no clean break here" * 20)
    with FakeDecisionServer(responses=[{
        "status": 200,
        "content": _not_done_content(reply=long_reply),
    }]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.reply_text.endswith("traveler!")
    assert len(turn.reply_text) <= 500


def test_converse_bad_json_content_raises():
    with FakeDecisionServer(responses=[{"status": 200, "content": "not json"}]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).converse(_history())


def test_converse_returns_private_reasoning_and_accepts_it_missing():
    with FakeDecisionServer(responses=[
        {"status": 200, "content": _done_content()},
        {"status": 200, "content": json.dumps({
            "done": False,
            "magic_type": "",
            "magic_level": 0,
            "reply": "Next question?",
        })},
    ]) as server:
        client = _client(server.url)
        assert client.converse(_history()).reasoning == "The guest is drawn to storms and bold choices."
        assert client.converse(_history()).reasoning == ""