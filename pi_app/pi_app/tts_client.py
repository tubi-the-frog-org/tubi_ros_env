"""Text-to-speech via the ElevenLabs API.

Uses a stock premade voice for now, steered toward a wizard-ish read
via voice_settings -- not a cloned custom voice yet. When a real
cloned "frog wizard" voice is ready, swap `voice_id` for the cloned
voice's id; TtsClient's shape (synthesize(text) -> bytes) doesn't
change either way.
"""

from __future__ import annotations
from elevenlabs.client import ElevenLabs
import os
from io import BytesIO


import requests

# "Adam" -- a default ElevenLabs premade voice available on every
# account. Swap for a cloned voice_id once one exists.
_DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"


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
        voice_id: str = _DEFAULT_VOICE_ID,
        stability: float = 0.5,
        similarity_boost: float = 0.6,
        timeout_s: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.voice_id = voice_id
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.timeout_s = timeout_s
        self._session = session or requests.Session()

    def custom_voice(self, ye_neh):
        if ye_neh:
            elevenlabs = ElevenLabs(
                api_key=os.getenv(self._api_key),
            )
            self.voice = elevenlabs.voices.ivc.create(
                name="Tubi's Voice",
                files = [BytesIO(open("pi_app/config/max.mp3").read())]
            )
    def synthesize(self, text: str) -> bytes:
        """Returns MP3 bytes, or raises a TtsError subtype."""
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
            },
        }
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            resp = self._session.post(
                f"{self.base_url}/text-to-speech/{self.voice_id}",
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
