from animatronic_common.snacks import Line, Snack, SnacksConfig
from animatronic_brain.cloud_client import CloudValidationError
from animatronic_brain.llm_provider import (
    SOURCE_CLOUD,
    SOURCE_FALLBACK,
    SOURCE_KEYWORD,
    SOURCE_LOCAL,
    CloudProvider,
    FallbackProvider,
    KeywordProvider,
    LocalProvider,
    build_provider_chain,
)
from animatronic_brain.ollama_client import OllamaError
from animatronic_brain.request_budget import RequestBudget

COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=["biscuit"], tags=["sweet"], lines=[Line(id="cookies_1", text="cookies!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, angle=195,
              aliases=["crisps"], tags=["salty", "crunchy"], lines=[Line(id="chips_1", text="chips!")])
NONE = Snack(id="none", label="No snack", slot=0, angle=0, aliases=[], tags=[], lines=[Line(id="none_1", text="nothing.")])
SNACKS = SnacksConfig(version=1, default_snack="none", snacks=[COOKIES, CHIPS, NONE])


class StubCloudClient:
    def __init__(self, result=None, exception=None):
        self.result = result
        self.exception = exception
        self.calls = 0

    def choose_snack_and_reply(self, user_text, snacks):
        self.calls += 1
        if self.exception:
            raise self.exception
        return self.result


class StubOllamaClient:
    def __init__(self, result=None, exception=None):
        self.result = result
        self.exception = exception
        self.calls = 0

    def choose_snack(self, user_text, snack_ids, system_prompt):
        self.calls += 1
        if self.exception:
            raise self.exception
        return self.result


def test_keyword_provider_decides_on_match():
    provider = KeywordProvider(threshold=1, margin=1)
    decision = provider.decide("r1", "something crunchy and salty", SNACKS)
    assert decision.snack_id == "chips"
    assert decision.source == SOURCE_KEYWORD
    assert decision.reply_text is None


def test_keyword_provider_defers_on_no_match():
    provider = KeywordProvider(threshold=1, margin=1)
    assert provider.decide("r2", "totally unrelated", SNACKS) is None


def test_cloud_provider_success():
    client = StubCloudClient(result=("cookies", "The cookies call to you!"))
    provider = CloudProvider(client, RequestBudget(), enabled=True)
    decision = provider.decide("r3", "anything", SNACKS)
    assert decision.snack_id == "cookies"
    assert decision.source == SOURCE_CLOUD
    assert decision.reply_text == "The cookies call to you!"


def test_cloud_provider_disabled_defers_without_calling_client():
    client = StubCloudClient(result=("cookies", "text"))
    provider = CloudProvider(client, RequestBudget(), enabled=False)
    assert provider.decide("r4", "anything", SNACKS) is None
    assert client.calls == 0


def test_cloud_provider_budget_exhausted_defers_without_calling_client():
    client = StubCloudClient(result=("cookies", "text"))
    budget = RequestBudget(daily_limit=0)
    provider = CloudProvider(client, budget, enabled=True)
    assert provider.decide("r5", "anything", SNACKS) is None
    assert client.calls == 0


def test_cloud_provider_error_defers_to_next_provider():
    client = StubCloudClient(exception=CloudValidationError("bad reply"))
    provider = CloudProvider(client, RequestBudget(), enabled=True)
    assert provider.decide("r6", "anything", SNACKS) is None


def test_local_provider_disabled_by_default_defers_without_calling_client():
    client = StubOllamaClient(result="cookies")
    provider = LocalProvider(client, enabled=False)
    assert provider.decide("r7", "anything", SNACKS) is None
    assert client.calls == 0


def test_local_provider_enabled_success():
    client = StubOllamaClient(result="chips")
    provider = LocalProvider(client, enabled=True)
    decision = provider.decide("r8", "anything", SNACKS)
    assert decision.snack_id == "chips"
    assert decision.source == SOURCE_LOCAL
    assert decision.reply_text is None  # local never writes its own line


def test_local_provider_error_defers_to_next_provider():
    client = StubOllamaClient(exception=OllamaError("boom"))
    provider = LocalProvider(client, enabled=True)
    assert provider.decide("r9", "anything", SNACKS) is None


def test_fallback_provider_always_decides():
    provider = FallbackProvider()
    decision = provider.decide("r10", "anything", SNACKS)
    assert decision.snack_id == "none"
    assert decision.source == SOURCE_FALLBACK
    assert decision.reply_text is None


def test_build_provider_chain_maps_names_in_order():
    providers_by_name = {
        "keyword": KeywordProvider(),
        "fallback": FallbackProvider(),
    }
    chain = build_provider_chain(["keyword", "fallback"], providers_by_name)
    assert [p.name for p in chain] == [SOURCE_KEYWORD, SOURCE_FALLBACK]


def test_build_provider_chain_skips_unknown_names():
    providers_by_name = {"fallback": FallbackProvider()}
    chain = build_provider_chain(["nonexistent", "fallback"], providers_by_name)
    assert [p.name for p in chain] == [SOURCE_FALLBACK]
