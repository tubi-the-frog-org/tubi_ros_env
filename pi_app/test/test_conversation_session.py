import pytest

from pi_app.conversation_session import (
    _FALLBACK_LEVEL,
    _FALLBACK_REPLY,
    _FALLBACK_TYPE,
    _FORCE_AFTER_TURNS,
    _NUDGE_AFTER_TURNS,
    ConversationSession,
    QuizResult,
)
from pi_app.decision_client import ConversationTurn, DecisionError, DecisionRequestError


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)  # consumed in order; the last one repeats if exhausted
        self.calls = []            # a snapshot of the message history for every call

    def converse(self, messages):
        self.calls.append([dict(m) for m in messages])
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def _talk(reply="Tell me about yourself!"):
    return ConversationTurn(reply_text=reply, done=False, magic_type=None, magic_level=None)


def _reveal(magic_type="Swamp Trickster", level=7, reply="Swamp Trickster, level seven."):
    return ConversationTurn(reply_text=reply, done=True, magic_type=magic_type, magic_level=level)


def test_start_returns_the_opening_line_and_is_not_done():
    client = StubDecisionClient([_talk("Greetings, traveler!")])
    session = ConversationSession("s1", client)
    assert session.start() == "Greetings, traveler!"
    assert session.done is False and session.result is None


def test_the_history_grows_and_starts_with_the_system_prompt():
    client = StubDecisionClient([_talk("Hello!"), _talk("Nice name.")])
    session = ConversationSession("s1", client)
    session.start()
    session.respond("I'm Ryan")
    second = client.calls[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[1]["content"] == "(session start)" and second[3]["content"] == "I'm Ryan"
    assert "magic_level" in second[0]["content"]   # the real system prompt, not a stub


def test_reaching_the_reveal_sets_done_and_the_result():
    client = StubDecisionClient([_talk("Hi!"), _reveal()])
    session = ConversationSession("s1", client)
    session.start()
    assert session.respond("I'd be an owl") == "Swamp Trickster, level seven."
    assert session.done is True
    assert session.result == QuizResult(
        session_id="s1", magic_type="Swamp Trickster", magic_level=7,
        transcript=[{"role": "user", "content": "(session start)"}, {"role": "assistant", "content": "Hi!"},
                    {"role": "user", "content": "I'd be an owl"}, {"role": "assistant", "content": "Swamp Trickster, level seven."}],
    )


def test_responding_after_the_reveal_raises():
    session = ConversationSession("s1", StubDecisionClient([_reveal()]))
    session.start()
    with pytest.raises(RuntimeError):
        session.respond("one more thing")


def test_a_failed_call_is_rolled_back_and_re_raised():
    client = StubDecisionClient([_talk("Hi!"), DecisionRequestError("boom"), _talk("Recovered.")])
    session = ConversationSession("s1", client)
    session.start()
    with pytest.raises(DecisionError):
        session.respond("this fails")
    assert session.respond("this works") == "Recovered."
    assert [m["content"] for m in client.calls[-1] if m["role"] == "user"] == ["(session start)", "this works"]


def test_a_failed_intro_can_be_retried():
    client = StubDecisionClient([DecisionRequestError("boom"), _talk("Hi!")])
    session = ConversationSession("s1", client)
    with pytest.raises(DecisionError):
        session.start()
    assert session.start() == "Hi!"


def _run(client, n_guest_turns):
    session = ConversationSession("s1", client)
    session.start()
    for i in range(n_guest_turns):
        if session.done:
            break
        session.respond(f"answer {i}")
    return session


def test_no_nudge_while_the_conversation_is_still_young():
    client = StubDecisionClient([_talk()])
    _run(client, _NUDGE_AFTER_TURNS - 1)
    assert all(not any("Internal note" in m["content"] for m in call) for call in client.calls)


def test_a_wrap_up_nudge_is_sent_once_the_model_has_asked_too_many_questions_but_never_kept():
    client = StubDecisionClient([_talk()])
    session = _run(client, _NUDGE_AFTER_TURNS)
    last = client.calls[-1]
    assert last[-1]["role"] == "system" and "done=true" in last[-1]["content"]
    assert not any("Internal note" in m["content"] for m in session._messages)   # one call only, never persisted


def test_a_model_that_never_reveals_is_finished_locally_with_no_further_call():
    client = StubDecisionClient([_talk()])
    session = _run(client, _FORCE_AFTER_TURNS + 1)
    calls_before = len(client.calls)
    assert session.done and session.result.magic_type == _FALLBACK_TYPE and session.result.magic_level == _FALLBACK_LEVEL
    assert session.result.transcript[-1] == {"role": "assistant", "content": _FALLBACK_REPLY}
    assert calls_before == 1 + _FORCE_AFTER_TURNS   # the intro + the turns the model got; the last one was local


def test_a_model_that_reveals_after_the_nudge_ends_normally():
    turns = [_talk()] * (_NUDGE_AFTER_TURNS) + [_reveal("Moon Mage", 9, "Moon Mage, level nine.")]
    session = _run(StubDecisionClient(turns), _NUDGE_AFTER_TURNS + 3)
    assert session.done and session.result.magic_type == "Moon Mage" and session.result.magic_level == 9
