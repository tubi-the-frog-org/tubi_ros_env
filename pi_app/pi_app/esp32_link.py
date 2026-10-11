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

import logging
import os
import time
from abc import ABC, abstractmethod

# ElevenLabs' default output is mp3_44100_128 = 128 kbit/s = 16000 bytes/s.
MP3_BYTES_PER_SECOND = 16000
MIN_WORD_MS = 120


def estimate_audio_seconds(audio_mp3: bytes, bytes_per_second: int = MP3_BYTES_PER_SECOND) -> float:
    return len(audio_mp3) / bytes_per_second


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


class _LineEspLink(EspLink):
    """The firmware's text protocol (one command per line); subclasses only
    decide how a line physically gets to the board."""

    def _send(self, line: str) -> None:
        raise NotImplementedError

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


class SerialEspLink(_LineEspLink):
    """The motor ESP32 over a USB-serial (or any real UART) device."""

    def __init__(self, port: str | None = None, baudrate: int = 115200, timeout_s: float = 2.0, serial_factory=None,
                 open_settle_s: float = 2.0, conn=None):
        if conn is not None:
            self._conn = conn  # a connection shared with the audio player (one-board firmware): already open
            return
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

    def close(self) -> None:
        self._conn.close()


class SoftSerialEspLink(_LineEspLink):
    """The motor ESP32 over ONE wire, no USB: the Pi bit-bangs 8N1 serial out of
    a spare GPIO (default GPIO18 = header pin 12) into the board's RX0 pin
    (GPIO3), plus a shared GND.

    The Pi's only hardware UART (pins 8/10) is busy with the audio link, and the
    motor commands only ever flow Pi -> board, so a transmit-only soft serial is
    enough. pigpio builds the waveform on the DMA engine, so 115200 baud timing
    is exact (software timing in Python would not be). Needs the pigpiod daemon.
    """

    _GPIO_OUTPUT = 1  # pigpio.OUTPUT, spelled out so tests don't need pigpio installed
    _BUSY_TIMEOUT_S = 2.0

    def __init__(self, gpio: int = 18, baudrate: int = 115200, pi=None, sleep=time.sleep):
        if pi is None:
            import pigpio  # pure-python client for the pigpiod daemon
            pi = pigpio.pi()
            if not pi.connected:
                raise OSError("could not reach pigpiod (start it: sudo systemctl enable --now pigpiod)")
        self._pi = pi
        self._gpio = gpio
        self._baud = baudrate
        self._sleep = sleep
        pi.set_mode(gpio, self._GPIO_OUTPUT)
        pi.write(gpio, 1)  # a UART line idles high

    def _wait_idle(self) -> None:
        waited = 0.0
        while self._pi.wave_tx_busy():
            if waited >= self._BUSY_TIMEOUT_S:
                raise OSError("previous soft-serial transmission never finished")
            self._sleep(0.002)
            waited += 0.002

    def _send(self, line: str) -> None:
        # The leading newline ends any half-received line the board may have
        # picked up from line noise; the firmware ignores empty lines.
        data = f"\n{line}\n".encode("ascii")
        self._wait_idle()
        self._pi.wave_clear()
        self._pi.wave_add_serial(self._gpio, self._baud, data)
        wave_id = self._pi.wave_create()
        if wave_id < 0:
            raise OSError(f"pigpio could not build the serial waveform (error {wave_id})")
        self._pi.wave_send_once(wave_id)

    def close(self) -> None:
        self._pi.stop()


def open_motor_link(environ=None, logger: logging.Logger | None = None) -> EspLink:
    """Picks the motor ESP32 link from the environment, never raising: if the
    configured link can't be opened, log it loudly and return a no-op link so
    the rest of Tubi (voice, conversation) keeps working without motion.

        ESP32_SOFT_TX_GPIO=18        one wire, no USB (see SoftSerialEspLink)
        ESP32_SERIAL_PORT=/dev/...   a USB-serial / UART device
    """
    environ = os.environ if environ is None else environ
    logger = logger or logging.getLogger(__name__)
    soft_gpio = environ.get("ESP32_SOFT_TX_GPIO")
    port = environ.get("ESP32_SERIAL_PORT")
    try:
        if soft_gpio:
            return SoftSerialEspLink(int(soft_gpio))
        if port:
            return SerialEspLink(port)
    except (OSError, ValueError, ImportError) as e:
        target = f"ESP32_SOFT_TX_GPIO={soft_gpio}" if soft_gpio else f"ESP32_SERIAL_PORT={port}"
        logger.error(f"could not open motor link ({target}): {e}; running with NO head/arm/door motion")
    return NullEspLink()
