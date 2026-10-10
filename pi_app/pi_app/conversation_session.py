"""One guest's multi-turn conversation with Tubi: intro -> guest's own
intro -> open-ended questions -> confirmation -> reveal with
percentages. The LLM itself drives the flow (what to ask, when enough
is known, when to reveal) via DecisionClient.converse() -- this class
just owns the growing chat history and turns the final answer into a
JSON-able QuizResult.

Percentages are the model's own holistic read of the whole
conversation, not a mechanical point tally -- in the spirit of the
IFT/EmoMap research this was asked to follow (open-ended elicitation
rather than forcing answers onto a fixed predetermined scale), though
it's worth being honest that an LLM's holistic judgment call is not
the same rigor as a validated psychometric instrument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_app.decision_client import DecisionClient, DecisionError, build_system_prompt
from pi_app.snacks import SnacksConfig

_FALLBACK_REPLY = "The fog clouds my sight for a moment... could you say that again, traveler?"
_START_PROMPT = (
    "(The guest has just arrived and hasn't said anything yet. Begin: introduce yourself with your usual "
    "mixed-up backstory, then ask them to introduce themselves.)"
)


@dataclass(frozen=True)
class QuizResult:
    session_id: str
    chosen_snack_id: str
    slot: int
    percentages: dict[str, float]
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
        self._messages: list[dict] = [{"role": "system", "content": build_system_prompt(snacks.snacks)}]
        self.done = False
        self.result: QuizResult | None = None

    def start(self) -> str:
        """Call once per session, before any guest speech, to get Tubi's opening line."""
        return self._step(_START_PROMPT)

    def respond(self, guest_text: str) -> str:
        if self.done:
            raise RuntimeError(f"session {self.session_id} already finished")
        return self._step(guest_text)

    def _step(self, user_text: str) -> str:
        self._messages.append({"role": "user", "content": user_text})

        try:
            turn = self._client.converse(self._messages, self._snacks.snacks)
        except DecisionError as e:
            self._logger.warning(f"[{self.session_id}] conversation call failed: {e}")
            self._messages.pop()  # don't poison history with a turn that never happened
            return _FALLBACK_REPLY

        self._messages.append({"role": "assistant", "content": turn.reply_text})

        if turn.done:
            self.done = True
            snack = self._snacks.get(turn.snack_id)
            self.result = QuizResult(
                session_id=self.session_id,
                chosen_snack_id=snack.id,
                slot=snack.slot,
                percentages=turn.percentages or {},
                transcript=[m for m in self._messages if m["role"] != "system"],
            )

        return turn.reply_text
