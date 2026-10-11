"""Orchestrates one request end to end: budget check -> decision call ->
character budget check -> TTS call -> MagicMessage. Never raises --
every failure degrades to a MagicMessage (with or without audio)
rather than an exception reaching the HTTP layer.
"""

from __future__ import annotations

import logging

from pi_app.decision_client import DecisionClient, DecisionError
from pi_app.message_package import (
    SOURCE_FALLBACK_BUDGET,
    SOURCE_FALLBACK_ERROR,
    SOURCE_OK,
    SOURCE_OK_NO_AUDIO,
    MagicMessage,
)
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.tts_client import TtsClient, TtsError

_BUDGET_EXCEEDED_REPLY = "The mists are resting... ask again in a moment, traveler."
_DECISION_ERROR_REPLY = "The fog clouds my sight for a moment... try again, traveler."


class DecisionService:
    def __init__(
        self,
        decision_client: DecisionClient,
        tts_client: TtsClient,
        request_budget: RequestBudget,
        character_budget: CharacterBudget,
        logger: logging.Logger | None = None,
    ):
        self._decision_client = decision_client
        self._tts_client = tts_client
        self._request_budget = request_budget
        self._character_budget = character_budget
        self._logger = logger or logging.getLogger(__name__)

    def handle(self, request_id: str, raw_text: str) -> MagicMessage:
        if not self._request_budget.try_consume():
            self._logger.warning(
                f"[{request_id}] decision request budget exhausted "
                f"(daily={self._request_budget.daily_count}/{self._request_budget.daily_limit}, "
                f"total={self._request_budget.total_count}/{self._request_budget.total_limit})"
            )
            return MagicMessage(request_id, "", 0, _BUDGET_EXCEEDED_REPLY, None, SOURCE_FALLBACK_BUDGET)

        try:
            magic_type, magic_level, reply_text = self._decision_client.assess_magic_user(raw_text)
        except DecisionError as e:
            self._logger.warning(f"[{request_id}] decision call failed: {e}")
            return MagicMessage(request_id, "", 0, _DECISION_ERROR_REPLY, None, SOURCE_FALLBACK_ERROR)

        audio = self._try_synthesize(request_id, reply_text)
        source = SOURCE_OK if audio is not None else SOURCE_OK_NO_AUDIO
        return MagicMessage(request_id, magic_type, magic_level, reply_text, audio, source)

    def _try_synthesize(self, request_id: str, reply_text: str) -> bytes | None:
        if not self._character_budget.try_consume(len(reply_text)):
            self._logger.warning(
                f"[{request_id}] TTS character budget exhausted "
                f"(daily={self._character_budget.daily_chars}/{self._character_budget.daily_limit}, "
                f"total={self._character_budget.total_chars}/{self._character_budget.total_limit}); "
                "sending text without audio"
            )
            return None

        try:
            return self._tts_client.synthesize(reply_text)
        except TtsError as e:
            self._logger.warning(f"[{request_id}] TTS call failed, sending text without audio: {e}")
            return None
