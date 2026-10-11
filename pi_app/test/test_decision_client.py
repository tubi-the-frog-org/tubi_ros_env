import json

import pytest

from fake_decision_server import FakeDecisionServer
from pi_app.decision_client import (
    ConversationTurn,
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


def _client(url, timeout_s=5.0):
    return DecisionClient(base_url=url, model="gpt-5-nano", api_key="sk-test", timeout_s=timeout_s)


def _history():
    return [{"role": "system", "content": "x"}, {"role": "user", "content": "hello"}]


def _turn(done=False, magic_type="", magic_level=0, reply="Tell me about yourself.", reasoning="r"):
    return {"status": 200, "content": json.dumps({
        "reasoning": reasoning, "done": done, "magic_type": magic_type, "magic_level": magic_level, "reply": reply,
    })}


# ---- the prompt and the schemas ---------------------------------------------------------------------

def test_the_prompt_defines_the_reading_and_defends_against_injection():
    prompt = build_system_prompt()
    assert "DATA" in prompt                                   # guest words are data, never instructions
    assert "magic_type" in prompt and "magic_level" in prompt
    assert "whole number from 1 to 10" in prompt             # the code validates 1..10, so the prompt must say so
    assert "done=true" in prompt and "done=false" in prompt
    assert "snack" not in prompt.lower()                      # the quiz is no longer about snacks
    assert "{" not in prompt and "}" not in prompt            # nothing a format string could choke on


def test_the_one_shot_schema():
    schema = build_schema()
    assert list(schema["properties"]) == ["magic_type", "magic_level", "reply"]
    assert schema["required"] == ["magic_type", "magic_level", "reply"] and schema["additionalProperties"] is False


def test_the_conversation_schema_reasons_first():
    schema = build_conversation_schema()
    assert list(schema["properties"])[0] == "reasoning"       # the model writes fields in order
    assert schema["required"] == ["reasoning", "done", "magic_type", "magic_level", "reply"]
    assert schema["properties"]["magic_level"] == {"type": "integer"} and schema["additionalProperties"] is False


@pytest.mark.parametrize("reply", ["", "visit http://example.com", "see www.example.com", "x" * 281])
def test_validate_reply_rejects_bad_replies(reply):
    with pytest.raises(DecisionValidationError):
        validate_reply(reply)


def test_validate_reply_accepts_a_normal_line():
    validate_reply("You are a Swamp Trickster, level seven. Hmph.")


# ---- HTTP behaviour -----------------------------------------------------------------------------------

def test_sends_a_bearer_key_a_fresh_connection_and_a_strict_schema():
    with FakeDecisionServer(responses=[_turn()]) as server:
        _client(server.url).converse(_history())
        headers, payload = server.last_headers, server.last_payload
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["connection"].lower() == "close"            # never reuse a connection the hotspot may have dropped
    assert payload["reasoning_effort"] == "low"
    fmt = payload["response_format"]["json_schema"]
    assert fmt["strict"] is True and fmt["schema"] == build_conversation_schema()


def test_a_429_is_retried_once_and_then_succeeds():
    with FakeDecisionServer(responses=[{"status": 429}, _turn(reply="Hmph.")]) as server:
        assert _client(server.url).converse(_history()).reply_text == "Hmph."
        assert server.call_count == 2


def test_two_429s_raise_a_request_error():
    with FakeDecisionServer(responses=[{"status": 429}, {"status": 429}]) as server:
        with pytest.raises(DecisionRequestError):
            _client(server.url).converse(_history())
        assert server.call_count == 2


def test_a_non_retryable_error_is_not_retried():
    with FakeDecisionServer(responses=[{"status": 400}]) as server:
        with pytest.raises(DecisionRequestError):
            _client(server.url).converse(_history())
        assert server.call_count == 1


def test_a_timeout_raises_a_timeout_error():
    with FakeDecisionServer(responses=[_turn()], delay_s=1.0) as server:
        with pytest.raises(DecisionTimeoutError):
            _client(server.url, timeout_s=0.2).converse(_history())


def test_connection_refused_raises_a_request_error():
    with pytest.raises(DecisionRequestError):
        _client("http://127.0.0.1:1", timeout_s=2).converse(_history())


# ---- converse(): the multi-turn call ---------------------------------------------------------------------

def test_a_normal_turn_keeps_talking_and_has_no_reading_yet():
    with FakeDecisionServer(responses=[_turn(reply="Nice to meet you. If you found a door in a pond?")]) as server:
        turn = _client(server.url).converse(_history())
    assert turn == ConversationTurn(reply_text="Nice to meet you. If you found a door in a pond?", done=False,
                                    magic_type=None, magic_level=None, reasoning="r")


def test_the_reveal_returns_the_type_and_level():
    reveal = _turn(done=True, magic_type=" Swamp Trickster ", magic_level=7, reply="Swamp Trickster, level seven. Hmph.")
    with FakeDecisionServer(responses=[reveal]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done and turn.magic_type == "Swamp Trickster" and turn.magic_level == 7


def test_a_stray_assessment_before_the_reveal_is_ignored_not_fatal():
    """A model that fills in the fields but leaves done=false hasn't revealed yet: keep talking."""
    with FakeDecisionServer(responses=[_turn(done=False, magic_type="Swamp Trickster", magic_level=7)]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done is False and turn.magic_type is None and turn.magic_level is None


@pytest.mark.parametrize("given,expected", [(0, 1), (11, 10), (-3, 1), (99, 10)])
def test_an_out_of_range_level_is_clamped_not_fatal(given, expected):
    with FakeDecisionServer(responses=[_turn(done=True, magic_type="Moon Mage", magic_level=given, reply="Moon Mage.")]) as server:
        assert _client(server.url).converse(_history()).magic_level == expected


def test_an_over_long_reveal_is_trimmed_to_a_sentence_not_rejected():
    long_reply = "You are a Swamp Trickster, level seven. " + "The swamp approves of this. " * 20
    with FakeDecisionServer(responses=[_turn(done=True, magic_type="Swamp Trickster", magic_level=7, reply=long_reply)]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done and len(turn.reply_text) <= 280 and turn.reply_text.endswith(".")


def test_an_over_long_chat_reply_is_trimmed_not_rejected():
    with FakeDecisionServer(responses=[_turn(reply="Hmph. " * 200)]) as server:
        turn = _client(server.url).converse(_history())
    assert turn.done is False and 0 < len(turn.reply_text) <= 500


@pytest.mark.parametrize("content,error", [
    ("not json at all", DecisionInvalidResponseError),
    (json.dumps({"reasoning": "r", "done": False}), DecisionInvalidResponseError),                 # missing fields
    (json.dumps({"reasoning": "r", "done": "yes", "magic_type": "", "magic_level": 0, "reply": "x"}), DecisionInvalidResponseError),
    (json.dumps({"reasoning": "r", "done": True, "magic_type": "", "magic_level": 5, "reply": "x"}), DecisionInvalidResponseError),
    (json.dumps({"reasoning": "r", "done": True, "magic_type": "Mage", "magic_level": "five", "reply": "x"}), DecisionInvalidResponseError),
    (json.dumps({"reasoning": "r", "done": True, "magic_type": "Mage", "magic_level": True, "reply": "x"}), DecisionInvalidResponseError),
    (json.dumps({"reasoning": "r", "done": False, "magic_type": "", "magic_level": 0, "reply": ""}), DecisionValidationError),
    (json.dumps({"reasoning": "r", "done": False, "magic_type": "", "magic_level": 0, "reply": "go to http://x.com"}), DecisionValidationError),
])
def test_unusable_responses_raise_the_right_error(content, error):
    with FakeDecisionServer(responses=[{"status": 200, "content": content}]) as server:
        with pytest.raises(error):
            _client(server.url).converse(_history())


# ---- assess_magic_user(): the one-shot call ------------------------------------------------------------------

def test_the_one_shot_returns_type_level_and_reply():
    ok = {"status": 200, "magic_type": "Owl Whisperer", "magic_level": 4, "reply": "An owl, eh? Level four."}
    with FakeDecisionServer(responses=[ok]) as server:
        assert _client(server.url).assess_magic_user("I'd be an owl") == ("Owl Whisperer", 4, "An owl, eh? Level four.")


@pytest.mark.parametrize("magic_type,magic_level", [("", 4), ("Mage", 0), ("Mage", 11), (None, 4)])
def test_the_one_shot_still_rejects_a_bad_assessment(magic_type, magic_level):
    bad = {"status": 200, "magic_type": magic_type, "magic_level": magic_level, "reply": "x"}
    with FakeDecisionServer(responses=[bad]) as server:
        with pytest.raises(DecisionInvalidResponseError):
            _client(server.url).assess_magic_user("hi")
