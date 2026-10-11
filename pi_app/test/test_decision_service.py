from pi_app.decision_client import DecisionError
from pi_app.decision_service import DecisionService
from pi_app.message_package import (
    SOURCE_FALLBACK_BUDGET,
    SOURCE_FALLBACK_ERROR,
    SOURCE_OK,
    SOURCE_OK_NO_AUDIO,
)
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.tts_client import TtsError


class StubDecisionClient:
    def __init__(self, result=None, exception=None):
        self.result = result
        self.exception = exception
        self.calls = 0

    def assess_magic_user(self, user_text):
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
        decision_client or StubDecisionClient(result=("Stormweaver", 7, "Your stormweaver spark is level seven!")),
        tts_client or StubTtsClient(),
        request_budget or RequestBudget(),
        character_budget or CharacterBudget(),
    )


def test_success_path_includes_assessment_and_audio():
    message = _service().handle("req-1", "anything")
    assert message.magic_type == "Stormweaver"
    assert message.magic_level == 7
    assert message.reply_text == "Your stormweaver spark is level seven!"
    assert message.audio_mp3 == b"mp3-bytes"
    assert message.source == SOURCE_OK


def test_request_budget_exhausted_skips_decision_and_tts_entirely():
    decision_client = StubDecisionClient(result=("Stormweaver", 7, "text"))
    tts_client = StubTtsClient()
    service = _service(decision_client, tts_client, request_budget=RequestBudget(daily_limit=0))

    message = service.handle("req-2", "anything")

    assert message.magic_type is None
    assert message.magic_level is None
    assert message.source == SOURCE_FALLBACK_BUDGET
    assert message.audio_mp3 is None
    assert decision_client.calls == 0
    assert tts_client.calls == []


def test_decision_error_falls_back_and_skips_tts():
    decision_client = StubDecisionClient(exception=DecisionError("boom"))
    tts_client = StubTtsClient()

    message = _service(decision_client, tts_client).handle("req-3", "anything")

    assert message.magic_type is None
    assert message.magic_level is None
    assert message.source == SOURCE_FALLBACK_ERROR
    assert message.audio_mp3 is None
    assert tts_client.calls == []


def test_character_budget_exhausted_keeps_assessment_but_skips_audio():
    message = _service(character_budget=CharacterBudget(daily_limit=0)).handle("req-4", "anything")

    assert message.magic_type == "Stormweaver"
    assert message.magic_level == 7
    assert message.audio_mp3 is None
    assert message.source == SOURCE_OK_NO_AUDIO


def test_tts_error_keeps_assessment_but_skips_audio():
    tts_client = StubTtsClient(exception=TtsError("boom"))
    message = _service(tts_client=tts_client).handle("req-5", "anything")

    assert message.magic_type == "Stormweaver"
    assert message.audio_mp3 is None
    assert message.source == SOURCE_OK_NO_AUDIO


def test_character_budget_consumes_exact_reply_length():
    character_budget = CharacterBudget(daily_limit=100)
    decision_client = StubDecisionClient(result=("Stormweaver", 7, "x" * 30))
    _service(decision_client, character_budget=character_budget).handle("req-6", "anything")

    assert character_budget.daily_chars == 30