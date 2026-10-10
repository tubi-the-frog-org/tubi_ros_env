"""Step 3 of the brain pipeline: ask Ollama to pick one snack id from
an enumerated JSON schema. Pure Python, no ROS dependency.
"""

from __future__ import annotations

import json

import requests

from animatronic_common.snacks import Snack

# Keep this prompt short (well under ~60 tokens) and otherwise identical
# on every call for a given snacks config, so Ollama's prompt cache hits.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a carnival wizard choosing a snack for a guest based on their "
    "spoken answer. Snacks: {snack_list}. Pick exactly one id."
)


class OllamaError(Exception):
    """Base class for all Ollama client failures."""


class OllamaTimeoutError(OllamaError):
    pass


class OllamaRequestError(OllamaError):
    """Connection refused, DNS failure, non-2xx status, etc."""


class OllamaInvalidResponseError(OllamaError):
    """Response was received but wasn't valid/parseable per the schema."""


def build_schema(snack_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": snack_ids},
        },
        "required": ["choice"],
    }


def build_system_prompt(snacks: list[Snack]) -> str:
    snack_list = ", ".join(f"{s.id} ({s.label})" for s in snacks)
    return _SYSTEM_PROMPT_TEMPLATE.format(snack_list=snack_list)


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 45.0,
        num_predict: int = 16,
        num_ctx: int = 1024,
        keep_alive: int = -1,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self._session = session or requests.Session()

    def _options(self) -> dict:
        return {
            "temperature": 0,
            "num_predict": self.num_predict,
            "num_ctx": self.num_ctx,
        }

    def _chat(self, messages: list[dict], format_schema: dict | None, num_predict: int | None = None) -> str:
        """POST /api/chat and return the raw message content string, or
        raise an OllamaError subtype."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {**self._options(), "num_predict": num_predict or self.num_predict},
        }
        if format_schema is not None:
            payload["format"] = format_schema

        try:
            resp = self._session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout_s,
            )
        except requests.exceptions.Timeout as e:
            raise OllamaTimeoutError(f"Ollama request timed out after {self.timeout_s}s") from e
        except requests.exceptions.RequestException as e:
            raise OllamaRequestError(f"Ollama request failed: {e}") from e

        if resp.status_code != 200:
            raise OllamaRequestError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
            return body["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise OllamaInvalidResponseError(f"could not parse Ollama response: {e}") from e

    def choose_snack(self, user_text: str, snack_ids: list[str], system_prompt: str) -> str:
        """Call /api/chat constrained to the given schema. Returns the
        chosen snack id, or raises an OllamaError subtype."""
        content = self._chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            format_schema=build_schema(snack_ids),
        )

        try:
            choice = json.loads(content)["choice"]
        except (ValueError, KeyError, TypeError) as e:
            raise OllamaInvalidResponseError(f"could not parse Ollama response: {e}") from e

        if choice not in snack_ids:
            raise OllamaInvalidResponseError(f"Ollama chose '{choice}', which is not in {snack_ids}")

        return choice

    def generate_prose_reply(self, user_text: str, snack: Snack, num_predict: int | None = None) -> str:
        """Experimental free-form path (llm_reply=true): ask the model to
        write the spoken line itself instead of using a pre-written one.
        Per measured facts, the 0.5B model is not expected to do this
        coherently — this exists for experimentation, not default use."""
        system_prompt = (
            "You are a carnival wizard. In one short dramatic sentence, "
            f"announce that the guest's snack is: {snack.label}."
        )
        content = self._chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            format_schema=None,
            num_predict=num_predict,
        )
        return content.strip()

    def health_check(self, timeout_s: float = 5.0) -> bool:
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=timeout_s)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def warm_up(self, snack_ids: list[str], system_prompt: str) -> bool:
        """Best-effort request to force the model into memory. Returns
        False (never raises) on failure so startup isn't blocked."""
        try:
            self.choose_snack("hello", snack_ids, system_prompt)
            return True
        except OllamaError:
            return False
