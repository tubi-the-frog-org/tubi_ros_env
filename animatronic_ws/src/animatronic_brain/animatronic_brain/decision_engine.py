"""Ties together steps 1-5 of the brain pipeline: normalize -> keyword
score -> Ollama -> fallback -> pick a line. One call per request, which
also leaves a clean extension point for a future multi-question quiz
(just call decide() once per answer and combine Decisions externally;
no quiz-specific state lives in here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from animatronic_common.snacks import SnacksConfig
from animatronic_brain.keyword_scorer import decide_keyword, normalize_text
from animatronic_brain.line_picker import LineHistory
from animatronic_brain.ollama_client import OllamaClient, OllamaError, build_system_prompt

SOURCE_KEYWORD = "keyword"
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "fallback"


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
        ollama_client: OllamaClient,
        keyword_threshold: int = 1,
        keyword_margin: int = 1,
        llm_reply: bool = False,
        line_history: LineHistory | None = None,
        logger: logging.Logger | None = None,
    ):
        self._ollama = ollama_client
        self._threshold = keyword_threshold
        self._margin = keyword_margin
        self._llm_reply = llm_reply
        self._lines = line_history or LineHistory()
        self._logger = logger or logging.getLogger(__name__)

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> Decision:
        normalized = normalize_text(raw_text)

        keyword_id = decide_keyword(normalized, snacks.snacks, self._threshold, self._margin)
        if keyword_id is not None:
            snack = snacks.get(keyword_id)
            source = SOURCE_KEYWORD
        else:
            system_prompt = build_system_prompt(snacks.snacks)
            try:
                llm_id = self._ollama.choose_snack(raw_text, snacks.ids(), system_prompt)
                snack = snacks.get(llm_id)
                source = SOURCE_LLM
            except OllamaError as e:
                self._logger.warning(f"[{request_id}] Ollama fallback triggered: {e}")
                snack = snacks.get(snacks.default_snack)
                source = SOURCE_FALLBACK

        if source == SOURCE_LLM and self._llm_reply:
            try:
                text = self._ollama.generate_prose_reply(raw_text, snack)
                line_id = "llm_generated"
            except OllamaError as e:
                self._logger.warning(f"[{request_id}] llm_reply generation failed, using pre-written line: {e}")
                line_id, text = self._lines.pick(snack, answer_text=raw_text)
        else:
            line_id, text = self._lines.pick(snack, answer_text=raw_text)

        return Decision(request_id=request_id, snack_id=snack.id, source=source, line_id=line_id, text=text)
