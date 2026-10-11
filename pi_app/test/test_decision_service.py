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

READING = ("Swamp Trickster", 7, "Swamp Trickster, level seven. Hmph.")


class StubDecisionClient:
    def __init__(self, result=READING, exception=None):
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
        decision_client or StubDecisionClient(),
        tts_client or StubTtsClient(),
        request_budget or RequestBudget(),
        character_budget or CharacterBudget(),
    )


def test_success_path_includes_audio():
    message = _service().handle("req-1", "anything")
    assert (message.magic_type, message.magic_level) == ("Swamp Trickster", 7)
    assert message.reply_text == "Swamp Trickster, level seven. Hmph."
    assert message.audio_mp3 == b"mp3-bytes"
    assert message.source == SOURCE_OK


def test_request_budget_exhausted_skips_decision_and_tts_entirely():
    decision_client, tts_client = StubDecisionClient(), StubTtsClient()
    service = _service(decision_client, tts_client, request_budget=RequestBudget(daily_limit=0))

    message = service.handle("req-2", "anything")

    assert (message.magic_type, message.magic_level) == ("", 0)   # no reading was made
    assert message.source == SOURCE_FALLBACK_BUDGET and message.audio_mp3 is None
    assert decision_client.calls == 0 and tts_client.calls == []


def test_decision_error_falls_back_and_skips_tts():
    tts_client = StubTtsClient()
    service = _service(StubDecisionClient(exception=DecisionError("boom")), tts_client)

    message = service.handle("req-3", "anything")

    assert (message.magic_type, message.magic_level) == ("", 0)
    assert message.source == SOURCE_FALLBACK_ERROR and message.audio_mp3 is None
    assert tts_client.calls == []


def test_character_budget_exhausted_keeps_the_reading_but_skips_audio():
    message = _service(character_budget=CharacterBudget(daily_limit=0)).handle("req-4", "anything")

    assert message.magic_type == "Swamp Trickster"   # the real reading is kept
    assert message.audio_mp3 is None and message.source == SOURCE_OK_NO_AUDIO


def test_tts_error_keeps_the_reading_but_skips_audio():
    message = _service(tts_client=StubTtsClient(exception=TtsError("boom"))).handle("req-5", "anything")

    assert message.magic_type == "Swamp Trickster" and message.magic_level == 7
    assert message.audio_mp3 is None and message.source == SOURCE_OK_NO_AUDIO


def test_character_budget_consumes_exact_reply_length():
    character_budget = CharacterBudget(daily_limit=100)
    decision_client = StubDecisionClient(result=("Moon Mage", 3, "x" * 30))

    _service(decision_client, character_budget=character_budget).handle("req-6", "anything")

    assert character_budget.daily_chars == 30
