"""Talks to the real ESP32 firmware (esp32_firmware/src/motor_esp2).

That firmware's app_main() reads ONE raw byte at a time off UART0 at
115200 baud -- no JSON, no header, no ack:
    b'1' -> trap door closes
    b'2' -> trap door opens
Anything else is ignored. There is no per-snack slot routing yet (the
firmware's LAZY_POS_1..4 constants for a rotating tray exist but
aren't wired to any input), so for now this can only open/close the
one door -- it can't yet pick a slot. SnackMessage.slot is carried
through the rest of the pipeline so that routing is a small addition
here once the firmware supports it, not a redesign.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

OPEN_BYTE = b"2"
CLOSE_BYTE = b"1"


class EspLink(ABC):
    @abstractmethod
    def open_trap_door(self) -> None:
        ...

    @abstractmethod
    def close_trap_door(self) -> None:
        ...


class NullEspLink(EspLink):
    """No-op link for dev/test when no ESP32 is attached."""

    def open_trap_door(self) -> None:
        pass

    def close_trap_door(self) -> None:
        pass


class SerialEspLink(EspLink):
    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 2.0, serial_factory=None):
        if serial_factory is None:
            import serial  # pyserial; imported lazily so tests don't need the real hardware lib
            serial_factory = serial.Serial
        self._conn = serial_factory(port, baudrate, timeout=timeout_s)

    def open_trap_door(self) -> None:
        self._conn.write(OPEN_BYTE)

    def close_trap_door(self) -> None:
        self._conn.write(CLOSE_BYTE)

    def close(self) -> None:
        self._conn.close()
