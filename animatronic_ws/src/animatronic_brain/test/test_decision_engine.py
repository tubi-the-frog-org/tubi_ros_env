from animatronic_common.snacks import Line, Snack, SnacksConfig
from animatronic_brain.decision_engine import (
    SOURCE_FALLBACK,
    SOURCE_KEYWORD,
    SOURCE_LLM,
    DecisionEngine,
)
from animatronic_brain.ollama_client import OllamaError

COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=["biscuit"], tags=["sweet", "soft"],
                 lines=[Line(id="cookies_1", text="cookies for {answer}!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, angle=195,
              aliases=["crisps"], tags=["salty", "crunchy"],
              lines=[Line(id="chips_1", text="chips!")])
NONE = Snack(id="none", label="No snack", slot=0, angle=0, aliases=[], tags=[],
             lines=[Line(id="none_1", text="nothing for you.")])

SNACKS = SnacksConfig(version=1, default_snack="none", snacks=[COOKIES, CHIPS, NONE])


class StubOllamaClient:
    def __init__(self, choose_result=None, choose_exception=None, prose_result=None, prose_exception=None):
        self.choose_result = choose_result
        self.choose_exception = choose_exception
        self.prose_result = prose_result
        self.prose_exception = prose_exception
        self.choose_calls = []
        self.prose_calls = []

    def choose_snack(self, user_text, snack_ids, system_prompt):
        self.choose_calls.append(user_text)
        if self.choose_exception:
            raise self.choose_exception
        return self.choose_result

    def generate_prose_reply(self, user_text, snack, num_predict=None):
        self.prose_calls.append(user_text)
        if self.prose_exception:
            raise self.prose_exception
        return self.prose_result


def test_keyword_match_short_circuits_ollama():
    client = StubOllamaClient()
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1)

    decision = engine.decide("req-1", "something crunchy and salty please", SNACKS)

    assert decision.snack_id == "chips"
    assert decision.source == SOURCE_KEYWORD
    assert decision.text == "chips!"
    assert client.choose_calls == []  # never fell through to the LLM


def test_llm_path_used_when_no_keyword_match():
    client = StubOllamaClient(choose_result="cookies")
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1)

    decision = engine.decide("req-2", "I feel like rain today", SNACKS)

    assert decision.snack_id == "cookies"
    assert decision.source == SOURCE_LLM
    assert decision.text == "cookies for I feel like rain today!"
    assert client.choose_calls == ["I feel like rain today"]


def test_fallback_path_used_on_ollama_error():
    client = StubOllamaClient(choose_exception=OllamaError("boom"))
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1)

    decision = engine.decide("req-3", "totally unrelated text", SNACKS)

    assert decision.snack_id == "none"
    assert decision.source == SOURCE_FALLBACK
    assert decision.text == "nothing for you."


def test_llm_reply_true_uses_generated_prose_on_llm_path():
    client = StubOllamaClient(choose_result="cookies", prose_result="A dramatic cookie announcement!")
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1, llm_reply=True)

    decision = engine.decide("req-4", "I feel like rain today", SNACKS)

    assert decision.source == SOURCE_LLM
    assert decision.text == "A dramatic cookie announcement!"
    assert decision.line_id == "llm_generated"
    assert client.prose_calls == ["I feel like rain today"]


def test_llm_reply_true_falls_back_to_pregenerated_line_on_prose_failure():
    client = StubOllamaClient(choose_result="cookies", prose_exception=OllamaError("boom"))
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1, llm_reply=True)

    decision = engine.decide("req-5", "I feel like rain today", SNACKS)

    assert decision.source == SOURCE_LLM
    assert decision.line_id == "cookies_1"
    assert "I feel like rain today" in decision.text


def test_llm_reply_true_has_no_effect_on_keyword_path():
    client = StubOllamaClient()
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1, llm_reply=True)

    decision = engine.decide("req-6", "something crunchy and salty please", SNACKS)

    assert decision.source == SOURCE_KEYWORD
    assert decision.text == "chips!"
    assert client.prose_calls == []


def test_fallback_path_does_not_consult_llm_reply():
    client = StubOllamaClient(choose_exception=OllamaError("boom"))
    engine = DecisionEngine(client, keyword_threshold=1, keyword_margin=1, llm_reply=True)

    decision = engine.decide("req-7", "totally unrelated text", SNACKS)

    assert decision.source == SOURCE_FALLBACK
    assert client.prose_calls == []
