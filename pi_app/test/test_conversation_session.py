from pi_app.conversation_session import ConversationSession, MagicResult
from pi_app.decision_client import ConversationTurn, DecisionError


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []

    def converse(self, messages):
        self.calls.append([dict(message) for message in messages])
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def _not_done(reply="Tell me about yourself!"):
    return ConversationTurn(reply_text=reply, done=False, magic_type=None, magic_level=None)


def _done(magic_type="Stormweaver", magic_level=7, reply="Your stormweaver spark is level seven!"):
    return ConversationTurn(
        reply_text=reply,
        done=True,
        magic_type=magic_type,
        magic_level=magic_level,
    )


def test_start_returns_first_reply_and_is_not_done():
    client = StubDecisionClient([_not_done("Greetings, traveler!")])
    session = ConversationSession("s1", client)

    assert session.start() == "Greetings, traveler!"
    assert session.done is False
    assert session.result is None


def test_respond_accumulates_history_across_turns():
    client = StubDecisionClient([_not_done("Hi there!"), _not_done("What's your favorite feeling?")])
    session = ConversationSession("s1", client)
    session.start()
    session.respond("I'm Max")

    assert [message["role"] for message in client.calls[-1]] == ["system", "user", "assistant", "user"]
    assert client.calls[-1][-1]["content"] == "I'm Max"


def test_reaching_done_sets_magic_result():
    client = StubDecisionClient([_not_done("Hi there!"), _done()])
    session = ConversationSession("s1", client)
    session.start()
    assert session.respond("I like thunderstorms") == "Your stormweaver spark is level seven!"

    assert session.done is True
    assert isinstance(session.result, MagicResult)
    assert session.result.magic_type == "Stormweaver"
    assert session.result.magic_level == 7
    assert session.result.transcript[0]["role"] == "user"
    assert all(message["role"] != "system" for message in session.result.transcript)


def test_respond_after_done_raises():
    client = StubDecisionClient([_done()])
    session = ConversationSession("s1", client)
    session.start()
    try:
        session.respond("anything")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_decision_error_propagates_without_poisoning_history():
    client = StubDecisionClient([_not_done("Hi!"), DecisionError("boom"), _not_done("Next question")])
    session = ConversationSession("s1", client)
    session.start()

    try:
        session.respond("something that fails")
        assert False, "expected DecisionError"
    except DecisionError:
        pass
    assert session.done is False

    assert session.respond("try again") == "Next question"
    assert client.calls[-1].count({"role": "user", "content": "something that fails"}) == 0


def test_only_system_prompt_controls_the_conversation():
    client = StubDecisionClient([_not_done(f"Question {i}") for i in range(12)])
    session = ConversationSession("s1", client)
    session.start()
    for i in range(10):
        session.respond(f"answer {i}")

    system_prompt = client.calls[0][0]
    assert system_prompt["role"] == "system"
    assert client.calls[0][1]["content"] == "(session start)"
    assert all(
        sum(message["role"] == "system" for message in call) == 1 and call[0] == system_prompt
        for call in client.calls
    )