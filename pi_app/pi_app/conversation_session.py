"""Manage conversation history and turn model results into a QuizResult.

The system prompt controls Tubi's dialogue and decisions. Snack percentages
are independent confidence scores, not a split of 100 across all snacks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_app.decision_client import DecisionClient, DecisionError, build_system_prompt
from pi_app.snacks import SnacksConfig

@dataclass(frozen=True)
class ChosenSnack:
    id: str
    slot: int
    label: str


@dataclass(frozen=True)
class QuizResult:
    session_id: str
    chosen_snacks: list[ChosenSnack]  # one or more, up to all configured snacks
    percentages: dict[str, float]  # independent 0-100 confidence per snack id, not a split of 100
    transcript: list[dict]  # [{"role": "user"|"assistant", "content": ...}, ...], no system prompt


class ConversationSession:
    def __init__(
        self,
        session_id: str,
        client: DecisionClient,
        snacks: SnacksConfig,
        logger: logging.Logger | None = None,
    ):
        self.session_id = session_id
        self._client = client
        self._snacks = snacks
        self._logger = logger or logging.getLogger(__name__)

        # The default/fallback snack (e.g. "none") is reserved for the
        # orchestration layer's own fallback (budget exhausted, DecisionError)
        # -- it's never a genuine quiz outcome. Excluding it from both the
        # prompt's listing and the schema's enum makes that a hard guarantee
        # (the model structurally can't pick it) rather than a soft
        # instruction it might ignore; it also sidesteps an awkward mention
        # check, since "No snack" doesn't naturally fit into a reveal line.
        self._choosable_snacks = [s for s in snacks.snacks if s.id != snacks.default_snack]
        if not self._choosable_snacks:
            self._logger.warning(f"[{session_id}] no non-default snacks configured; falling back to the full list")
            self._choosable_snacks = snacks.snacks

        self._messages: list[dict] = [{"role": "system", "content": build_system_prompt(self._choosable_snacks)}]
        self.done = False
        self.result: QuizResult | None = None

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
            turn = self._client.converse(self._messages, self._choosable_snacks)
        except DecisionError as e:
            self._messages.pop()  # don't poison history with a turn that never happened
            self._logger.warning(f"[{self.session_id}] conversation call failed: {e}")
            raise

        self._messages.append({"role": "assistant", "content": turn.reply_text})

        if turn.done:
            self.done = True
            chosen = [self._snacks.get(sid) for sid in turn.snack_ids]
            self.result = QuizResult(
                session_id=self.session_id,
                chosen_snacks=[ChosenSnack(id=s.id, slot=s.slot, label=s.label) for s in chosen],
                percentages=turn.percentages or {},
                transcript=[m for m in self._messages if m["role"] != "system"],
            )

        return turn.reply_text
