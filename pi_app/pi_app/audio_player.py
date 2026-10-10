"""Plays mp3 bytes out of whatever speaker is wired to the Pi itself.

The ESP32 firmware has no audio playback code (no I2S/DAC driver), so
"Tubi's speaker" is the Pi's own audio output, not something relayed
through the ESP32. This shells out to a player binary rather than
pulling in a Python audio library, to keep the dependency/RAM
footprint small. Never raises -- a playback failure (missing player,
bad hardware) degrades to "no audio played," same as upstream TTS
failures already do.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import tempfile


class AudioPlayer:
    def __init__(self, player_cmd: list[str] | None = None, logger: logging.Logger | None = None):
        self._player_cmd = player_cmd or self._detect_player()
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _detect_player() -> list[str] | None:
        if platform.system() == "Darwin" and shutil.which("afplay"):
            return ["afplay"]
        if shutil.which("mpg123"):
            return ["mpg123", "-q"]
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        return None

    def play(self, audio_mp3: bytes) -> None:
        if not audio_mp3:
            return
        if self._player_cmd is None:
            self._logger.warning("no audio player found (afplay/mpg123/ffplay); skipping playback")
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(audio_mp3)
            f.flush()
            try:
                subprocess.run([*self._player_cmd, f.name], check=True, capture_output=True)
            except (subprocess.CalledProcessError, OSError) as e:
                self._logger.warning(f"audio playback failed: {e}")
