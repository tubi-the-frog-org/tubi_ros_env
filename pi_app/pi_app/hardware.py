"""Opens the Pi's side of the ESP32 wiring from the environment (secrets.env).

Three layouts are supported:

  One board, one wire (current): the Pi's UART (/dev/serial0, pins 8/10) goes straight to the
  one-board firmware, which plays the audio AND runs the servos. Audio and motor commands share
  that single connection:
      AUDIO_SERIAL_PORT=/dev/serial0
      ESP32_SERIAL_PORT=/dev/serial0      (same device as the audio port -> shared automatically)
      AUDIO_ACK_BYTE=6                    (the one-board firmware acks a finished clip with 0x06)

  Two links: audio on the Pi's UART (via the S3 bridge, ack 'D') and the motor board on USB:
      AUDIO_SERIAL_PORT=/dev/serial0
      ESP32_SERIAL_PORT=/dev/serial/by-path/...   (a different device)

  No audio port: speech plays from the Pi's own speaker (mpg123).

Nothing here raises: an unopenable port is logged and that part degrades (no motion / Pi speaker)
instead of taking the whole service down.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

from pi_app.audio_player import AudioPlayer, Esp32AudioPlayer
from pi_app.esp32_link import EspLink, NullEspLink, SerialEspLink, open_motor_link


class LockedSerial:
    """One serial connection used by two writers (the audio player and the motor link).

    Every write takes a lock, so two threads can never interleave bytes. `transaction()` holds the
    lock across several writes: the audio player uses it so a motor command can never land in the
    middle of an audio frame. Reads are not locked (pyserial allows a read and a write at once).
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.RLock()

    def write(self, data: bytes):
        with self._lock:
            return self._conn.write(data)

    def read(self, size: int = 1) -> bytes:
        return self._conn.read(size)

    def reset_input_buffer(self) -> None:
        self._conn.reset_input_buffer()

    def flush(self) -> None:
        with self._lock:
            self._conn.flush()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            yield


def _same_device(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def open_hardware(environ=None, bytes_per_second: int = 4000, logger: logging.Logger | None = None,
                  serial_module=None) -> tuple[EspLink, object]:
    """Returns (motor_link, audio_player). Both always work: they may be no-ops / the Pi speaker."""
    environ = os.environ if environ is None else environ
    logger = logger or logging.getLogger(__name__)
    audio_port = environ.get("AUDIO_SERIAL_PORT")
    motor_port = environ.get("ESP32_SERIAL_PORT")
    soft_gpio = environ.get("ESP32_SOFT_TX_GPIO")
    try:
        baud = int(environ.get("AUDIO_BAUD") or 921600)
        ack = bytes([int(environ.get("AUDIO_ACK_BYTE") or ord("D"))])
    except ValueError as e:
        logger.error(f"bad AUDIO_BAUD / AUDIO_ACK_BYTE ({e}); using 921600 baud and ack 'D'")
        baud, ack = 921600, b"D"

    shared = None
    if audio_port and motor_port and not soft_gpio and _same_device(audio_port, motor_port):
        try:
            if serial_module is None:
                import serial as serial_module
            shared = LockedSerial(serial_module.Serial(audio_port, baud, timeout=1.0))
            logger.info(f"audio and motor commands share one link on {audio_port} @ {baud}")
        except (OSError, ValueError) as e:  # serial.SerialException is an OSError
            logger.error(f"could not open the shared ESP32 link {audio_port} ({e}); no audio and no motion from it")

    player = AudioPlayer()
    if shared is not None:
        player = Esp32AudioPlayer(conn=shared, bytes_per_second=bytes_per_second, ack=ack)
        return SerialEspLink(conn=shared), player

    if audio_port:
        try:
            player = Esp32AudioPlayer(audio_port, baud, bytes_per_second, ack=ack)
        except (OSError, ValueError) as e:
            logger.error(f"could not open AUDIO_SERIAL_PORT {audio_port} ({e}); falling back to the Pi's own speaker")

    return open_motor_link(environ, logger), player
