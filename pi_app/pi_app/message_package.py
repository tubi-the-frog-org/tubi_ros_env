"""The package the Pi hands to the ESP32: a snack choice and the mp3
audio to speak. audio_mp3 is None when TTS was skipped or failed --
the ESP32 side (and anything else downstream) must handle that by
just not playing audio, not by treating it as an error."""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_OK = "ok"
SOURCE_OK_NO_AUDIO = "ok_no_audio"
SOURCE_FALLBACK_BUDGET = "fallback_budget"
SOURCE_FALLBACK_ERROR = "fallback_error"


@dataclass(frozen=True)
class SnackMessage:
    request_id: str
    snack_id: str
    reply_text: str
    audio_mp3: bytes | None
    source: str
