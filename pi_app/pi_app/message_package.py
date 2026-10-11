"""The package the one-shot pipeline hands off: the guest's magic reading and the mp3 audio to speak.
audio_mp3 is None when TTS was skipped or failed -- downstream consumers must handle that by just not
playing audio, not by treating it as an error.
"""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_OK = "ok"
SOURCE_OK_NO_AUDIO = "ok_no_audio"
SOURCE_FALLBACK_BUDGET = "fallback_budget"
SOURCE_FALLBACK_ERROR = "fallback_error"


@dataclass(frozen=True)
class MagicMessage:
    request_id: str
    magic_type: str  # "" on a fallback (no reading was made)
    magic_level: int  # 1..10, or 0 on a fallback
    reply_text: str
    audio_mp3: bytes | None
    source: str
