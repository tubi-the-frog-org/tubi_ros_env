"""Manage conversation history and turn model results into a MagicResult."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_app.decision_client import DecisionClient, DecisionError, build_system_prompt

@dataclass(frozen=True)
class MagicResult:
    session_id: str
    magic_type: str
    magic_level: int
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
        self.done = False
        self.result: MagicResult | None = None

    def start(self) -> str:
        """Call once per session, before any guest speech, to get Tubi's opening line."""
        return self._step("(session start)")

    def respond(self, guest_text: str) -> str:
        if self.done:
            raise RuntimeError(f"session {self.session_id} already finished")
        return self._step(guest_text)

    def _step(self, user_text: str) -> str:
        self._messages.append({"role": "user", "content": user_text})

        try:
            turn = self._client.converse(self._messages)
        except DecisionError as e:
            self._messages.pop()  # don't poison history with a turn that never happened
            self._logger.warning(f"[{self.session_id}] conversation call failed: {e}")
            raise

        self._messages.append({"role": "assistant", "content": turn.reply_text})

        if turn.done:
            self.done = True
            self.result = MagicResult(
                session_id=self.session_id,
                magic_type=turn.magic_type,
                magic_level=turn.magic_level,
                transcript=[m for m in self._messages if m["role"] != "system"],
            )

        return turn.reply_text
