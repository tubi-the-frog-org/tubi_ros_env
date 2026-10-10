from pi_app.decision_client import DecisionError
from pi_app.decision_service import DecisionService
from pi_app.message_package import (
    SOURCE_FALLBACK_BUDGET,
    SOURCE_FALLBACK_ERROR,
    SOURCE_OK,
    SOURCE_OK_NO_AUDIO,
)
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.snacks import Line, Snack, SnacksConfig
from pi_app.tts_client import TtsError

COOKIES = Snack(id="cookies", label="Cookies", slot=1, aliases=[], tags=[], lines=[])
NONE = Snack(id="none", label="No snack", slot=0, aliases=[], tags=[], lines=[])
SNACKS = SnacksConfig(version=1, default_snack="none", snacks=[COOKIES, NONE])


class StubDecisionClient:
    def __init__(self, result=None, exception=None):
        self.result = result
        self.exception = exception
        self.calls = 0

    def choose_snack_and_reply(self, user_text, snacks):
        self.calls += 1
        if self.exception:
            raise self.exception
        return self.result


class StubTtsClient:
    def __init__(self, result=b"mp3-bytes", exception=None):
        self.result = result
        self.exception = exception
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.exception:
            raise self.exception
        return self.result


def _service(decision_client=None, tts_client=None, request_budget=None, character_budget=None):
    return DecisionService(
        decision_client or StubDecisionClient(result=("cookies", "The cookies call to you!")),
        tts_client or StubTtsClient(),
        request_budget or RequestBudget(),
        character_budget or CharacterBudget(),
    )


def test_success_path_includes_audio():
    service = _service()
    message = service.handle("req-1", "anything", SNACKS)
    assert message.snack_id == "cookies"
    assert message.slot == 1
    assert message.reply_text == "The cookies call to you!"
    assert message.audio_mp3 == b"mp3-bytes"
    assert message.source == SOURCE_OK


def test_request_budget_exhausted_skips_decision_and_tts_entirely():
    decision_client = StubDecisionClient(result=("cookies", "text"))
    tts_client = StubTtsClient()
    service = _service(decision_client, tts_client, request_budget=RequestBudget(daily_limit=0))

    message = service.handle("req-2", "anything", SNACKS)

    assert message.snack_id == "none"  # default_snack
    assert message.slot == 0
    assert message.source == SOURCE_FALLBACK_BUDGET
    assert message.audio_mp3 is None
    assert decision_client.calls == 0
    assert tts_client.calls == []


def test_decision_error_falls_back_and_skips_tts():
    decision_client = StubDecisionClient(exception=DecisionError("boom"))
    tts_client = StubTtsClient()
    service = _service(decision_client, tts_client)

    message = service.handle("req-3", "anything", SNACKS)

    assert message.snack_id == "none"
    assert message.slot == 0
    assert message.source == SOURCE_FALLBACK_ERROR
    assert message.audio_mp3 is None
    assert tts_client.calls == []


def test_character_budget_exhausted_keeps_decision_but_skips_audio():
    service = _service(character_budget=CharacterBudget(daily_limit=0))

    message = service.handle("req-4", "anything", SNACKS)

    assert message.snack_id == "cookies"  # the real decision is kept
    assert message.reply_text == "The cookies call to you!"
    assert message.audio_mp3 is None
    assert message.source == SOURCE_OK_NO_AUDIO


def test_tts_error_keeps_decision_but_skips_audio():
    tts_client = StubTtsClient(exception=TtsError("boom"))
    service = _service(tts_client=tts_client)

    message = service.handle("req-5", "anything", SNACKS)

    assert message.snack_id == "cookies"
    assert message.audio_mp3 is None
    assert message.source == SOURCE_OK_NO_AUDIO


def test_character_budget_consumes_exact_reply_length():
    character_budget = CharacterBudget(daily_limit=100)
    decision_client = StubDecisionClient(result=("cookies", "x" * 30))
    service = _service(decision_client, character_budget=character_budget)

    service.handle("req-6", "anything", SNACKS)

    assert character_budget.daily_chars == 30
