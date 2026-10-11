"""Talks to the ESP32 firmware (esp32_firmware/src/motor_esp2).

The firmware reads ONE text command per line off UART0 at 115200 baud
(see the protocol comment at the top of its pwm.c):
    dispense <1-4>        the final decision: rotate to that bin, then the
                          trap door opens and closes quickly, on its own
    bin <1-4>             lazy susan to that bin
    door open | close     trap door
    talk <ms> <ms> ...    per-word durations; the ESP32 bobs the head up/down
                          on its own for each word (and raises the arm)
This replaces the old single-byte protocol (b'1'/b'2'), which the new
firmware no longer understands.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

# ElevenLabs' default output is mp3_44100_128 = 128 kbit/s = 16000 bytes/s.
MP3_BYTES_PER_SECOND = 16000
MIN_WORD_MS = 120


def estimate_audio_seconds(audio_mp3: bytes) -> float:
    return len(audio_mp3) / MP3_BYTES_PER_SECOND


def word_durations_ms(text: str, audio_seconds: float) -> list[int]:
    """Split the audio's length across the words, weighted by word length.

    Pure estimate -- we have word counts and total audio time, not real
    word timestamps. Close enough for a head bob.
    """
    words = text.split()
    if not words or audio_seconds <= 0:
        return []
    weights = [len(w) + 1 for w in words]  # +1 ~ the pause after each word
    total = sum(weights)
    return [max(MIN_WORD_MS, round(audio_seconds * 1000 * w / total)) for w in weights]


class EspLink(ABC):
    @abstractmethod
    def open_trap_door(self) -> None:
        ...

    @abstractmethod
    def close_trap_door(self) -> None:
        ...

    @abstractmethod
    def set_slot(self, slot: int) -> None:
        """Rotate the lazy susan to bin `slot` (1-4)."""

    @abstractmethod
    def talk(self, word_ms: list[int]) -> None:
        """Start the head/arm talking animation; returns immediately."""

    @abstractmethod
    def dispense(self, slot: int) -> None:
        """Rotate to bin `slot` (1-4), open and close the trap door; returns immediately."""


class NullEspLink(EspLink):
    """No-op link for dev/test when no ESP32 is attached."""

    def open_trap_door(self) -> None:
        pass

    def close_trap_door(self) -> None:
        pass

    def set_slot(self, slot: int) -> None:
        pass

    def talk(self, word_ms: list[int]) -> None:
        pass

    def dispense(self, slot: int) -> None:
        pass


class SerialEspLink(EspLink):
    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 2.0, serial_factory=None,
                 open_settle_s: float = 2.0):
        real_port = serial_factory is None
        if real_port:
            import serial  # pyserial; imported lazily so tests don't need the real hardware lib
            serial_factory = serial.Serial
        self._conn = serial_factory(port, baudrate, timeout=timeout_s)
        if real_port:
            # Opening a USB-serial port toggles DTR/RTS, which resets a DOIT
            # devkit; give it time to reboot before the first command.
            time.sleep(open_settle_s)

    def _send(self, line: str) -> None:
        self._conn.write(f"{line}\n".encode("ascii"))

    def open_trap_door(self) -> None:
        self._send("door open")

    def close_trap_door(self) -> None:
        self._send("door close")

    def set_slot(self, slot: int) -> None:
        self._send(f"bin {slot}")

    def dispense(self, slot: int) -> None:
        self._send(f"dispense {slot}")

    def talk(self, word_ms: list[int]) -> None:
        if word_ms:
            self._send("talk " + " ".join(str(m) for m in word_ms[:64]))

    def close(self) -> None:
        self._conn.close()
