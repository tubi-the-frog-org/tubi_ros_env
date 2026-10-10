"""The brain pipeline is a chain of LlmProvider instances, tried in
order (ROS param `provider_chain`, default [keyword, cloud, fallback])
until one returns a decision. Each provider either decides or returns
None to defer to the next provider -- it never raises out of decide().
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from animatronic_common.snacks import SnacksConfig

from animatronic_brain.cloud_client import CloudLlmClient, CloudLlmError
from animatronic_brain.keyword_scorer import decide_keyword, normalize_text
from animatronic_brain.ollama_client import OllamaClient, OllamaError, build_system_prompt
from animatronic_brain.request_budget import RequestBudget

SOURCE_KEYWORD = "keyword"
SOURCE_CLOUD = "cloud"
SOURCE_LOCAL = "local"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class ProviderDecision:
    snack_id: str
    source: str
    reply_text: str | None  # set only when the provider wrote the spoken line itself (cloud)


class LlmProvider(ABC):
    name: str

    @abstractmethod
    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> ProviderDecision | None:
        """Return a decision, or None to defer to the next provider."""


class KeywordProvider(LlmProvider):
    name = SOURCE_KEYWORD

    def __init__(self, threshold: int = 1, margin: int = 1):
        self._threshold = threshold
        self._margin = margin

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> ProviderDecision | None:
        normalized = normalize_text(raw_text)
        snack_id = decide_keyword(normalized, snacks.snacks, self._threshold, self._margin)
        if snack_id is None:
            return None
        return ProviderDecision(snack_id=snack_id, source=SOURCE_KEYWORD, reply_text=None)


class CloudProvider(LlmProvider):
    name = SOURCE_CLOUD

    def __init__(
        self,
        client: CloudLlmClient,
        budget: RequestBudget,
        enabled: bool = True,
        logger: logging.Logger | None = None,
    ):
        self._client = client
        self._budget = budget
        self._enabled = enabled
        self._logger = logger or logging.getLogger(__name__)

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> ProviderDecision | None:
        if not self._enabled:
            self._logger.debug(f"[{request_id}] cloud provider disabled (kill switch)")
            return None

        if not self._budget.try_consume():
            self._logger.warning(
                f"[{request_id}] cloud request budget exhausted "
                f"(daily={self._budget.daily_count}/{self._budget.daily_limit}, "
                f"total={self._budget.total_count}/{self._budget.total_limit})"
            )
            return None

        try:
            snack_id, reply = self._client.choose_snack_and_reply(raw_text, snacks.snacks)
        except CloudLlmError as e:
            self._logger.warning(f"[{request_id}] cloud provider failed, trying next provider: {e}")
            return None

        return ProviderDecision(snack_id=snack_id, source=SOURCE_CLOUD, reply_text=reply)


class LocalProvider(LlmProvider):
    name = SOURCE_LOCAL

    def __init__(self, client: OllamaClient, enabled: bool = False, logger: logging.Logger | None = None):
        self._client = client
        self._enabled = enabled
        self._logger = logger or logging.getLogger(__name__)

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> ProviderDecision | None:
        if not self._enabled:
            return None

        try:
            snack_id = self._client.choose_snack(raw_text, snacks.ids(), build_system_prompt(snacks.snacks))
        except OllamaError as e:
            self._logger.warning(f"[{request_id}] local provider failed, trying next provider: {e}")
            return None

        return ProviderDecision(snack_id=snack_id, source=SOURCE_LOCAL, reply_text=None)


class FallbackProvider(LlmProvider):
    name = SOURCE_FALLBACK

    def decide(self, request_id: str, raw_text: str, snacks: SnacksConfig) -> ProviderDecision | None:
        return ProviderDecision(snack_id=snacks.default_snack, source=SOURCE_FALLBACK, reply_text=None)


def build_provider_chain(names: list[str], providers_by_name: dict[str, LlmProvider]) -> list[LlmProvider]:
    """Map the ROS param's provider name list to provider instances,
    skipping unknown names (logged by the caller if desired)."""
    chain = []
    for name in names:
        provider = providers_by_name.get(name)
        if provider is not None:
            chain.append(provider)
    return chain
