"""Runs the configured LlmProvider chain for one request, then fills in
a pre-written line unless the provider already wrote the spoken text
itself (cloud). One call per request -- leaves a clean extension point
for a future multi-question quiz (call decide() once per answer and
combine Decisions externally; no quiz-specific state lives in here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from animatronic_common.snacks import SnacksConfig

from animatronic_brain.line_picker import LineHistory
from animatronic_brain.llm_provider import SOURCE_FALLBACK, LlmProvider


@dataclass(frozen=True)
class Decision:
    request_id: str
    snack_id: str
    source: str
    line_id: str
    text: str


class DecisionEngine:
    def __init__(
        self,
        provider_chain: list[LlmProvider],
        line_history: LineHistory | None = None,
        logger: logging.Logger | None = None,
    ):
        self._chain = provider_chain
        self._lines = line_history or LineHistory()
        self._logger = logger or logging.getLogger(__name__)

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> Decision:
        result = None
        for provider in self._chain:
            result = provider.decide(request_id, raw_text, snacks)
            if result is not None:
                break

        if result is None:
            # Misconfigured chain (no provider, or none included `fallback`) --
            # guarantee termination anyway rather than ever leaving a request undecided.
            self._logger.error(f"[{request_id}] provider chain exhausted with no decision; hard-fallback")
            snack = snacks.get(snacks.default_snack)
            line_id, text = self._lines.pick(snack, answer_text=raw_text)
            return Decision(request_id=request_id, snack_id=snack.id, source=SOURCE_FALLBACK, line_id=line_id, text=text)

        snack = snacks.get(result.snack_id)
        if result.reply_text is not None:
            return Decision(
                request_id=request_id,
                snack_id=snack.id,
                source=result.source,
                line_id=f"{result.source}_generated",
                text=result.reply_text,
            )

        line_id, text = self._lines.pick(snack, answer_text=raw_text)
        return Decision(request_id=request_id, snack_id=snack.id, source=result.source, line_id=line_id, text=text)
