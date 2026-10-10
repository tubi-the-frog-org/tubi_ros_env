"""Sends a SnackMessage to the ESP32: a newline-delimited JSON header
(request_id, snack_id, audio byte count) followed by that many raw
audio bytes if present. This is a minimal placeholder protocol, not
the real spec -- esp32_bridge/docs/esp32_protocol.md (still unwritten)
is where the actual wire protocol, chunking/flow-control for audio,
and ESP32-side ack/heartbeat handling belong. The point here is just
giving the Pi side a clean, testable interface to write against.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from pi_app.message_package import SnackMessage


class EspLink(ABC):
    @abstractmethod
    def send(self, message: SnackMessage) -> None:
        ...


class NullEspLink(EspLink):
    """No-op link for dev/test when no ESP32 is attached."""

    def send(self, message: SnackMessage) -> None:
        pass


class SerialEspLink(EspLink):
    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 2.0, serial_factory=None):
        if serial_factory is None:
            import serial  # pyserial; imported lazily so tests don't need the real hardware lib
            serial_factory = serial.Serial
        self._conn = serial_factory(port, baudrate, timeout=timeout_s)

    def send(self, message: SnackMessage) -> None:
        header = {
            "request_id": message.request_id,
            "snack_id": message.snack_id,
            "audio_len": len(message.audio_mp3) if message.audio_mp3 else 0,
        }
        self._conn.write((json.dumps(header) + "\n").encode("utf-8"))
        if message.audio_mp3:
            self._conn.write(message.audio_mp3)

    def close(self) -> None:
        self._conn.close()
