from animatronic_common.snacks import Line, Snack, SnacksConfig
from animatronic_brain.decision_engine import DecisionEngine
from animatronic_brain.llm_provider import (
    SOURCE_CLOUD,
    SOURCE_FALLBACK,
    SOURCE_KEYWORD,
    FallbackProvider,
    KeywordProvider,
    ProviderDecision,
)

COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=["biscuit"], tags=["sweet", "soft"],
                 lines=[Line(id="cookies_1", text="cookies for {answer}!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, angle=195,
              aliases=["crisps"], tags=["salty", "crunchy"],
              lines=[Line(id="chips_1", text="chips!")])
NONE = Snack(id="none", label="No snack", slot=0, angle=0, aliases=[], tags=[],
             lines=[Line(id="none_1", text="nothing for you.")])

SNACKS = SnacksConfig(version=1, default_snack="none", snacks=[COOKIES, CHIPS, NONE])


class StubProvider:
    """A provider that returns a fixed decision (or None) and records
    whether it was consulted, for asserting short-circuit behavior."""

    def __init__(self, name, result):
        self.name = name
        self._result = result
        self.called = False

    def decide(self, request_id, raw_text, snacks):
        self.called = True
        return self._result


def test_first_provider_to_decide_wins_and_short_circuits():
    first = StubProvider("keyword", ProviderDecision(snack_id="chips", source=SOURCE_KEYWORD, reply_text=None))
    second = StubProvider("cloud", ProviderDecision(snack_id="cookies", source=SOURCE_CLOUD, reply_text="nope"))
    engine = DecisionEngine([first, second])

    decision = engine.decide("req-1", "anything", SNACKS)

    assert decision.snack_id == "chips"
    assert decision.source == SOURCE_KEYWORD
    assert decision.text == "chips!"
    assert second.called is False


def test_falls_through_providers_that_defer():
    deferring = StubProvider("keyword", None)
    deciding = StubProvider("cloud", ProviderDecision(snack_id="cookies", source=SOURCE_CLOUD, reply_text="A cookie omen!"))
    engine = DecisionEngine([deferring, deciding])

    decision = engine.decide("req-2", "anything", SNACKS)

    assert decision.snack_id == "cookies"
    assert decision.source == SOURCE_CLOUD
    assert decision.text == "A cookie omen!"
    assert decision.line_id == "cloud_generated"


def test_reply_text_none_falls_back_to_pregenerated_line_with_templating():
    provider = StubProvider("keyword", ProviderDecision(snack_id="cookies", source=SOURCE_KEYWORD, reply_text=None))
    engine = DecisionEngine([provider])

    decision = engine.decide("req-3", "I feel like rain today", SNACKS)

    assert decision.text == "cookies for I feel like rain today!"
    assert decision.line_id == "cookies_1"


def test_empty_chain_hard_falls_back_without_crashing():
    engine = DecisionEngine([])

    decision = engine.decide("req-4", "anything", SNACKS)

    assert decision.snack_id == "none"
    assert decision.source == SOURCE_FALLBACK
    assert decision.text == "nothing for you."


def test_chain_of_all_deferring_providers_hard_falls_back():
    engine = DecisionEngine([StubProvider("keyword", None), StubProvider("cloud", None)])

    decision = engine.decide("req-5", "anything", SNACKS)

    assert decision.snack_id == "none"
    assert decision.source == SOURCE_FALLBACK


def test_real_keyword_and_fallback_providers_end_to_end():
    engine = DecisionEngine([KeywordProvider(threshold=1, margin=1), FallbackProvider()])

    keyword_hit = engine.decide("req-6", "something crunchy and salty", SNACKS)
    assert keyword_hit.source == SOURCE_KEYWORD
    assert keyword_hit.snack_id == "chips"

    no_match = engine.decide("req-7", "totally unrelated text", SNACKS)
    assert no_match.source == SOURCE_FALLBACK
    assert no_match.snack_id == "none"
