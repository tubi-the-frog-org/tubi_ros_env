"""Manage conversation history and turn the model's final turn into a MagicReading.

The system prompt controls Tubi's dialogue and when he gives the guest their "magic type and level"
reading. The code only adds a safety net so a demo can never hang on an uncooperative model: a nudge
after a handful of guest turns, then a built-in reading if the model still hasn't given one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_app.decision_client import DecisionClient, DecisionError, build_system_prompt

# The prompt asks for at most four questions (so the reveal comes on the guest's 5th answer). The model
# was measured ignoring that and asking 10+ questions, so the code backs it up: a one-off reminder after
# _NUDGE_AFTER_TURNS guest turns, and a built-in reading after _FORCE_AFTER_TURNS if it still hasn't revealed.
_NUDGE_AFTER_TURNS = 6
_FORCE_AFTER_TURNS = 8
_WRAP_UP_NUDGE = (
    "(Internal note, not from the guest: you've asked enough questions. Do not ask another one. "
    "Set done=true this turn, pick a magic type and a magic level from 1 to 10, and give the reading now.)"
)
_FALLBACK_TYPE = "Mysterious Swamp Wanderer"
_FALLBACK_LEVEL = 5
_FALLBACK_REPLY = (
    "My crystal ball flickers and settles... you are a Mysterious Swamp Wanderer, magic level five. "
    "Don't let it go to your head."
)


@dataclass(frozen=True)
class QuizResult:
    session_id: str
    magic_type: str  # a short, imaginative description, e.g. "Swamp Trickster"
    magic_level: int  # 1 (novice) .. 10 (master)
    transcript: list[dict]  # [{"role": "user"|"assistant", "content": ...}, ...], no system prompt


class ConversationSession:
    def __init__(
        self,
        session_id: str,
        client: DecisionClient,
        logger: logging.Logger | None = None,
    ):
        self.session_id = session_id
        self._client = client
        self._logger = logger or logging.getLogger(__name__)
        self._messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]
        self._turn_count = 0  # guest turns after the intro
        self.done = False
        self.result: QuizResult | None = None

    def start(self) -> str:
        """Call once per session, before any guest speech, to get Tubi's opening line."""
        return self._step("(session start)", is_intro=True)

    def respond(self, guest_text: str) -> str:
        if self.done:
            raise RuntimeError(f"session {self.session_id} already finished")
        return self._step(guest_text)

    def _step(self, user_text: str, is_intro: bool = False) -> str:
        if not is_intro:
            self._turn_count += 1

        if self._turn_count > _FORCE_AFTER_TURNS:
            # The model had its chance (and a nudge). Don't spend another call: give a reading and end.
            self._logger.warning(f"[{self.session_id}] no reading after {self._turn_count - 1} turns; finishing locally")
            self._messages.append({"role": "user", "content": user_text})
            self._messages.append({"role": "assistant", "content": _FALLBACK_REPLY})
            self._finish(_FALLBACK_TYPE, _FALLBACK_LEVEL)
            return _FALLBACK_REPLY

        self._messages.append({"role": "user", "content": user_text})
        send = self._messages
        if not is_intro and self._turn_count >= _NUDGE_AFTER_TURNS:
            send = [*self._messages, {"role": "system", "content": _WRAP_UP_NUDGE}]  # this call only, never kept

        try:
            turn = self._client.converse(send)
        except DecisionError as e:
            self._messages.pop()  # don't poison history with a turn that never happened
            if not is_intro:
                self._turn_count -= 1
            self._logger.warning(f"[{self.session_id}] conversation call failed: {e}")
            raise

        self._messages.append({"role": "assistant", "content": turn.reply_text})
        if turn.done:
            self._finish(turn.magic_type, turn.magic_level)
        return turn.reply_text

    def _finish(self, magic_type: str, magic_level: int) -> None:
        self.done = True
        self.result = QuizResult(
            session_id=self.session_id,
            magic_type=magic_type,
            magic_level=magic_level,
            transcript=[m for m in self._messages if m["role"] != "system"],
        )
