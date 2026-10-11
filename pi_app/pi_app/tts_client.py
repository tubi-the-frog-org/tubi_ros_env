"""Text-to-speech via the ElevenLabs API.

Uses a stock premade voice by default, steered toward a wizard-ish
read via voice_settings. To use a cloned voice instead, run
tools/clone_voice.py once (a separate one-time setup action, not
something this client does at runtime) and set TTS_VOICE_ID to the
id it prints. TtsClient's shape (synthesize(text) -> bytes) doesn't
change either way -- voice_id is just a constructor argument.
"""

from __future__ import annotations

import logging

import requests

# "Adam" -- a default ElevenLabs premade voice available on every
# account. Swap for a cloned voice_id once one exists.
_DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_logger = logging.getLogger(__name__)


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
        output_format: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.voice_id = voice_id
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.timeout_s = timeout_s
        self._session = session or requests.Session()
        # ElevenLabs query param, e.g. "mp3_22050_32". The ESP32 audio firmware
        # plays at a fixed 22050 Hz, so the mp3 it receives must be 22.05 kHz.
        self.output_format = output_format

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
            # A fresh connection per request. Reusing a pooled one is what hung this call on the hotspot:
            # the NAT silently drops idle connections, and the next request on a dead one waits out the
            # whole timeout. A voice line is requested every ~10 s at most, so keep-alive saves little.
            "Connection": "close",
        }
        params = {"output_format": self.output_format} if self.output_format else None
        url = f"{self.base_url}/text-to-speech/{self.voice_id}"

        # One retry (on a fresh connection) for a timeout, a connection error, or a 429/5xx: a hiccup
        # shouldn't leave the guest staring at a silent frog.
        for attempt in (1, 2):
            try:
                resp = self._session.post(url, json=payload, headers=headers, params=params, timeout=self.timeout_s)
            except requests.exceptions.Timeout as e:
                if attempt == 1:
                    _logger.warning(f"TTS request timed out after {self.timeout_s}s; retrying once")
                    continue
                raise TtsTimeoutError(f"TTS request timed out after {self.timeout_s}s (twice)") from e
            except requests.exceptions.RequestException as e:
                if attempt == 1:
                    _logger.warning(f"TTS request failed ({e}); retrying once")
                    continue
                raise TtsRequestError(f"TTS request failed: {e}") from e
            if resp.status_code in _RETRYABLE_STATUS and attempt == 1:
                _logger.warning(f"TTS returned HTTP {resp.status_code}; retrying once")
                continue
            break

        if resp.status_code != 200:
            raise TtsRequestError(f"TTS call returned HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.content

    @property
    def bytes_per_second(self) -> int:
        """Audio bytes per second of speech for the configured mp3 format."""
        parts = (self.output_format or "").split("_")  # mp3_<hz>_<kbps>
        if len(parts) == 3 and parts[0] == "mp3" and parts[2].isdigit():
            return int(parts[2]) * 125
        return 16000  # ElevenLabs default: mp3_44100_128
