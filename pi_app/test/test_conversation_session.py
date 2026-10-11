from pi_app.conversation_session import (
    ConversationSession,
    QuizResult,
)
from pi_app.decision_client import ConversationTurn, DecisionError
from pi_app.snacks import Line, Snack, SnacksConfig

COOKIES = Snack(id="cookies", label="Cookies", slot=1, aliases=[], tags=["sweet"], lines=[Line(id="c1", text="cookies!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, aliases=[], tags=["salty"], lines=[Line(id="h1", text="chips!")])
SNACKS = SnacksConfig(version=1, default_snack="cookies", snacks=[COOKIES, CHIPS])


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)  # consumed in order; last one repeats if exhausted
        self.calls = []  # list of message-history snapshots, one per call

    def converse(self, messages, snacks):
        self.calls.append([dict(m) for m in messages])
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def _not_done(reply="Tell me about yourself!"):
    return ConversationTurn(reply_text=reply, done=False, snack_ids=[], percentages=None)


def _done(snack_ids=("chips",), reply="Chips await!", percentages=None):
    return ConversationTurn(
        reply_text=reply,
        done=True,
        snack_ids=list(snack_ids),
        percentages=percentages or {"cookies": 25.0, "chips": 75.0},
    )


def test_start_returns_first_reply_and_is_not_done():
    client = StubDecisionClient([_not_done("Greetings, traveler!")])
    session = ConversationSession("s1", client, SNACKS)

    reply = session.start()

    assert reply == "Greetings, traveler!"
    assert session.done is False
    assert session.result is None


def test_respond_accumulates_history_across_turns():
    client = StubDecisionClient([_not_done("Hi there!"), _not_done("What's your favorite feeling?")])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    session.respond("I'm Max")

    # system + start-prompt + assistant + user("I'm Max") + assistant
    roles = [m["role"] for m in client.calls[-1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert client.calls[-1][-1]["content"] == "I'm Max"


def test_respond_reaching_done_sets_result():
    client = StubDecisionClient([
        _not_done("Hi there!"),
        _done(snack_ids=("chips",), reply="Chips for you!", percentages={"cookies": 10.0, "chips": 90.0}),
    ])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    reply = session.respond("yes, reveal it")

    assert reply == "Chips for you!"
    assert session.done is True
    assert isinstance(session.result, QuizResult)
    assert len(session.result.chosen_snacks) == 1
    assert session.result.chosen_snacks[0].id == "chips"
    assert session.result.chosen_snacks[0].slot == 3
    assert session.result.chosen_snacks[0].label == "Chips"
    assert session.result.percentages == {"cookies": 10.0, "chips": 90.0}
    assert session.result.transcript[0]["role"] == "user"
    assert all(m["role"] != "system" for m in session.result.transcript)


def test_respond_reaching_done_can_choose_multiple_snacks():
    client = StubDecisionClient([
        _not_done("Hi there!"),
        _done(snack_ids=("cookies", "chips"), reply="Both cookies and chips suit you!", percentages={"cookies": 70.0, "chips": 65.0}),
    ])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    session.respond("yes, reveal it")

    assert [s.id for s in session.result.chosen_snacks] == ["cookies", "chips"]
    assert session.result.percentages == {"cookies": 70.0, "chips": 65.0}


def test_respond_after_done_raises():
    client = StubDecisionClient([_done()])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    try:
        session.respond("anything")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_decision_error_propagates_without_poisoning_history():
    client = StubDecisionClient([_not_done("Hi!"), DecisionError("boom"), _not_done("Next question")])
    session = ConversationSession("s1", client, SNACKS)
    session.start()

    try:
        session.respond("something that fails")
        assert False, "expected DecisionError"
    except DecisionError:
        pass
    assert session.done is False

    reply2 = session.respond("try again")
    assert reply2 == "Next question"
    last_history = client.calls[-1]
    assert last_history.count({"role": "user", "content": "something that fails"}) == 0


def test_only_system_prompt_controls_the_conversation():
    client = StubDecisionClient([_not_done(f"Question {i}") for i in range(12)])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    for i in range(10):
        session.respond(f"answer {i}")

    system_prompt = client.calls[0][0]
    assert system_prompt["role"] == "system"
    assert client.calls[0][1]["content"] == "(session start)"
    assert all(
        sum(message["role"] == "system" for message in call) == 1
        and call[0] == system_prompt
        for call in client.calls
    )
