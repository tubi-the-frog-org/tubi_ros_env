from pi_app.conversation_session import _GOOFY_RETRY_REPLIES, _WRAP_UP_NUDGE, ConversationSession, QuizResult
from pi_app.decision_client import ConversationTurn, DecisionError
from pi_app.snacks import Line, Snack, SnacksConfig

COOKIES = Snack(id="cookies", label="Cookies", slot=1, aliases=[], tags=["sweet"], lines=[Line(id="c1", text="cookies!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, aliases=[], tags=["salty"], lines=[Line(id="h1", text="chips!")])
SNACKS = SnacksConfig(version=1, default_snack="cookies", snacks=[COOKIES, CHIPS])


class StubDecisionClient:
    def __init__(self, turns):
        self._turns = list(turns)  # consumed in order; last one repeats if exhausted
        self.calls = []  # list of message-history snapshots, one per call
        self.force_done_flags = []  # the force_done value passed on each call

    def converse(self, messages, snacks, force_done=False):
        self.calls.append([dict(m) for m in messages])
        self.force_done_flags.append(force_done)
        turn = self._turns[min(len(self.calls) - 1, len(self._turns) - 1)]
        if isinstance(turn, Exception):
            raise turn
        return turn


def _not_done(reply="Tell me about yourself!"):
    return ConversationTurn(reply_text=reply, done=False, snack_id=None, percentages=None)


def _done(snack_id="chips", reply="Chips await!", percentages=None):
    return ConversationTurn(reply_text=reply, done=True, snack_id=snack_id, percentages=percentages or {"cookies": 25.0, "chips": 75.0})


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
        _done(snack_id="chips", reply="Chips for you!", percentages={"cookies": 10.0, "chips": 90.0}),
    ])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    reply = session.respond("yes, reveal it")

    assert reply == "Chips for you!"
    assert session.done is True
    assert isinstance(session.result, QuizResult)
    assert session.result.chosen_snack_id == "chips"
    assert session.result.slot == 3
    assert session.result.percentages == {"cookies": 10.0, "chips": 90.0}
    assert session.result.transcript[0] == {"role": "user", "content": "(The guest has just arrived and hasn't said anything yet. Begin: introduce yourself with your usual mixed-up backstory, then ask them to introduce themselves.)"}
    assert all(m["role"] != "system" for m in session.result.transcript)


def test_respond_after_done_raises():
    client = StubDecisionClient([_done()])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    try:
        session.respond("anything")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_decision_error_falls_back_without_poisoning_history():
    client = StubDecisionClient([_not_done("Hi!"), DecisionError("boom"), _not_done("Let's try again, shall we?")])
    session = ConversationSession("s1", client, SNACKS)
    session.start()

    reply = session.respond("something that fails")
    assert reply in _GOOFY_RETRY_REPLIES
    assert session.done is False

    # the failed turn's user message should have been rolled back, so the
    # next call's history doesn't contain a dangling unanswered user turn
    reply2 = session.respond("try again")
    assert reply2 == "Let's try again, shall we?"
    last_history = client.calls[-1]
    assert last_history.count({"role": "user", "content": "something that fails"}) == 0


def test_intro_does_not_count_toward_the_question_turn_limit():
    client = StubDecisionClient([_not_done()])
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    assert session._turn_count == 0


def test_nudge_is_injected_after_turn_threshold_but_not_persisted_to_history():
    turns = [_not_done(f"Question {i}") for i in range(10)]
    client = StubDecisionClient(turns)
    session = ConversationSession("s1", client, SNACKS)
    session.start()

    for i in range(4):
        session.respond(f"answer {i}")
    assert client.calls[-1][-1] != {"role": "system", "content": _WRAP_UP_NUDGE}  # not yet at the threshold

    session.respond("answer 4")  # the 5th respond() call -- turn_count was 4 going in
    assert client.calls[-1][-1] == {"role": "system", "content": _WRAP_UP_NUDGE}
    # the nudge must never leak into the session's own persisted history
    assert all(m.get("content") != _WRAP_UP_NUDGE for m in session._messages)


def test_force_done_passed_once_hard_turn_limit_is_reached():
    turns = [_not_done(f"Question {i}") for i in range(10)]
    client = StubDecisionClient(turns)
    session = ConversationSession("s1", client, SNACKS)
    session.start()

    for i in range(6):
        session.respond(f"answer {i}")
    assert client.force_done_flags[-1] is False  # not yet at the hard limit

    session.respond("answer 6")  # the 7th respond() call -- turn_count was 6 going in
    assert client.force_done_flags[-1] is True


def test_forced_reveal_that_still_fails_finalizes_locally_instead_of_looping():
    # +1 for start()'s own turn; then enough not_done turns to cross the hard
    # limit, then a DecisionError on the forced-done attempt itself (e.g. the
    # model said done=true but snack="undecided" even with done locked true).
    turns = [_not_done(f"Question {i}") for i in range(7)] + [DecisionError("done=true but snack is undecided")]
    client = StubDecisionClient(turns)
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    for i in range(6):
        session.respond(f"answer {i}")

    reply = session.respond("answer 6")  # the 7th respond() -- force_done=True, and the call itself fails

    assert client.force_done_flags[-1] is True
    assert session.done is True
    assert isinstance(session.result, QuizResult)
    assert session.result.chosen_snack_id == "chips"  # the only non-default choosable snack in SNACKS
    assert session.result.percentages == {"chips": 100.0}
    assert "chips" in reply.lower()


def test_force_done_turn_that_comes_back_done_finalizes_normally():
    # +1 for start()'s own turn, which doesn't count toward the question limit
    turns = [_not_done(f"Question {i}") for i in range(7)] + [_done(snack_id="chips")]
    client = StubDecisionClient(turns)
    session = ConversationSession("s1", client, SNACKS)
    session.start()
    for i in range(6):
        session.respond(f"answer {i}")

    reply = session.respond("answer 6")

    assert client.force_done_flags[-1] is True
    assert session.done is True
    assert reply == "Chips await!"
