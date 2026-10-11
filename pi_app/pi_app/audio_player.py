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
import struct
import subprocess
import tempfile
import time


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


class Esp32AudioPlayer:
    """Streams mp3 bytes to the ESP32 audio firmware over a UART.

    Protocol (esp32_firmware/src/dac_music_decoder/src/main.c):
        'A' 'U' 'D' | uint32 length (little-endian) | mp3 bytes
    and the ESP32 answers a single 'D' once the track has finished playing.

    The ESP32 has a 32 KB receive buffer and no flow control is wired, so
    after an initial burst the bytes are paced just above the audio's own
    bitrate. Same contract as AudioPlayer: play() never raises, and returns
    only once the speech has finished (so "speaking" ends when it's quiet).
    """

    BURST_BYTES = 8 * 1024
    CHUNK_BYTES = 1024
    PACE_FACTOR = 1.1  # send 10% faster than playback

    def __init__(
        self,
        port: str,
        baudrate: int = 921600,
        bytes_per_second: int = 4000,  # mp3_22050_32
        serial_factory=None,
        sleep=time.sleep,
        logger: logging.Logger | None = None,
    ):
        if serial_factory is None:
            import serial  # pyserial; lazy so tests/dev machines don't need hardware
            serial_factory = serial.Serial
        self._conn = serial_factory(port, baudrate, timeout=1.0)
        self._bytes_per_second = bytes_per_second
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)

    def play(self, audio_mp3: bytes) -> None:
        if not audio_mp3:
            return
        try:
            self._stream(audio_mp3)
        except Exception as e:  # serial errors must degrade to "no audio", never crash a turn
            self._logger.warning(f"ESP32 audio playback failed: {e}")

    def _stream(self, audio: bytes) -> None:
        self._conn.reset_input_buffer()
        self._conn.write(b"AUD" + struct.pack("<I", len(audio)))
        self._conn.write(audio[: self.BURST_BYTES])
        pace_s = self.CHUNK_BYTES / (self._bytes_per_second * self.PACE_FACTOR)
        for i in range(self.BURST_BYTES, len(audio), self.CHUNK_BYTES):
            self._conn.write(audio[i : i + self.CHUNK_BYTES])
            self._sleep(pace_s)

        # Wait for the ESP32's 'D' (track finished); bail out after the
        # audio's expected length plus slack so a dead link can't hang a turn.
        deadline = time.monotonic() + len(audio) / self._bytes_per_second + 3.0
        while time.monotonic() < deadline:
            if self._conn.read(1) == b"D":
                return
        self._logger.warning("ESP32 never acknowledged end of track (wiring? baud rate?)")
