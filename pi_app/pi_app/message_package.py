"""The one-shot magic assessment and optional generated audio reply."""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_OK = "ok"
SOURCE_OK_NO_AUDIO = "ok_no_audio"
SOURCE_FALLBACK_BUDGET = "fallback_budget"
SOURCE_FALLBACK_ERROR = "fallback_error"


@dataclass(frozen=True)
class MagicMessage:
    request_id: str
    magic_type: str | None
    magic_level: int | None
    reply_text: str
    audio_mp3: bytes | None
    source: str
