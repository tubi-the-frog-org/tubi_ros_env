"""The package the Pi hands off: which snack (by id and physical slot
number) and the mp3 audio to speak. audio_mp3 is None when TTS was
skipped or failed -- downstream consumers must handle that by just not
playing audio, not by treating it as an error.

slot is a plain int, not an angle -- the ESP32 firmware doesn't take
an arbitrary angle, it has a small number of hardcoded positions
(currently just open/closed on one trap door; a real per-slot mapping
is still pending on the firmware side). slot is carried here so that
mapping can be wired up later without changing this shape again.
"""

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
    slot: int
    reply_text: str
    audio_mp3: bytes | None
    source: str
