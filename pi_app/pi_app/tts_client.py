"""Text-to-speech via an OpenAI-compatible /audio/speech endpoint.

Note: this uses a stock voice + `instructions` to steer tone (mystical
carnival wizard), not a cloned custom voice -- OpenAI's TTS endpoint
only offers a fixed voice list. If a genuinely custom cloned voice
matters more than staying on one provider/key, swap this client for an
ElevenLabs-style voice-cloning provider instead; the DecisionService
only depends on the synthesize(text) -> bytes shape below, so that's a
drop-in replacement, not a redesign.
"""

from __future__ import annotations

import requests

_DEFAULT_INSTRUCTIONS = (
    "Speak like a mystical carnival wizard addressing a guest: warm, "
    "theatrical, a little dramatic, but still clear and brief."
)


class TtsError(Exception):
    """Base class for all TTS failures."""


class TtsTimeoutError(TtsError):
    pass


class TtsRequestError(TtsError):
    """Connection failure or a non-2xx status."""


class TtsClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        voice: str = "fable",
        instructions: str | None = _DEFAULT_INSTRUCTIONS,
        timeout_s: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.voice = voice
        self.instructions = instructions
        self.timeout_s = timeout_s
        self._session = session or requests.Session()

    def synthesize(self, text: str) -> bytes:
        """Returns MP3 bytes, or raises a TtsError subtype."""
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "mp3",
        }
        if self.instructions:
            payload["instructions"] = self.instructions

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = self._session.post(
                f"{self.base_url}/audio/speech",
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        except requests.exceptions.Timeout as e:
            raise TtsTimeoutError(f"TTS request timed out after {self.timeout_s}s") from e
        except requests.exceptions.RequestException as e:
            raise TtsRequestError(f"TTS request failed: {e}") from e

        if resp.status_code != 200:
            raise TtsRequestError(f"TTS call returned HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.content
