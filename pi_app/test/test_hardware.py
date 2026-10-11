import logging
import os

import pytest

from pi_app.audio_player import AudioPlayer, Esp32AudioPlayer
from pi_app.esp32_link import NullEspLink, SerialEspLink
from pi_app.hardware import LockedSerial, open_hardware


class FakeConn:
    def __init__(self, replies=b""):
        self.written = []
        self.replies = list(replies)

    def write(self, data): self.written.append(bytes(data)); return len(data)
    def read(self, n=1): return bytes([self.replies.pop(0)]) if self.replies else b""
    def reset_input_buffer(self): pass
    def flush(self): pass
    def close(self): pass


class FakeSerialModule:
    """Records which devices were opened; raises for any in `broken`."""

    def __init__(self, broken=()):
        self.opened, self.broken = [], set(broken)

    def Serial(self, port, baud, timeout=1.0):
        if port in self.broken:
            raise OSError(f"no such device {port}")
        self.opened.append((port, baud))
        return FakeConn()


ONE_BOARD = {"AUDIO_SERIAL_PORT": "/dev/serial0", "ESP32_SERIAL_PORT": "/dev/serial0", "AUDIO_ACK_BYTE": "6"}


def test_one_board_layout_opens_ONE_port_and_shares_it_between_audio_and_motors():
    fake = FakeSerialModule()
    link, player = open_hardware(ONE_BOARD, serial_module=fake)

    assert fake.opened == [("/dev/serial0", 921600)]          # one open, not two
    assert isinstance(link, SerialEspLink) and isinstance(player, Esp32AudioPlayer)
    assert link._conn is player._conn and isinstance(link._conn, LockedSerial)
    assert player._ack == b"\x06"


def test_same_device_under_two_names_is_still_shared(tmp_path):
    real = tmp_path / "ttyAMA0"
    real.write_text("")
    alias = tmp_path / "serial0"
    os.symlink(real, alias)                                    # /dev/serial0 -> /dev/ttyAMA0 on the Pi
    fake = FakeSerialModule()
    link, player = open_hardware({"AUDIO_SERIAL_PORT": str(alias), "ESP32_SERIAL_PORT": str(real)}, serial_module=fake)
    assert len(fake.opened) == 1 and link._conn is player._conn


def test_different_devices_are_two_independent_links(monkeypatch):
    opened = []
    import pi_app.hardware as hw
    monkeypatch.setattr(hw, "Esp32AudioPlayer", lambda port, baud, bps, ack=b"D": ("audio", port, baud, ack))
    monkeypatch.setattr(hw, "open_motor_link", lambda environ, logger: ("motor", environ["ESP32_SERIAL_PORT"]))
    link, player = open_hardware({"AUDIO_SERIAL_PORT": "/dev/serial0", "ESP32_SERIAL_PORT": "/dev/ttyUSB0"})
    assert player == ("audio", "/dev/serial0", 921600, b"D")   # S3-bridge default ack stays 'D'
    assert link == ("motor", "/dev/ttyUSB0")


def test_unopenable_shared_port_degrades_instead_of_crashing(caplog):
    fake = FakeSerialModule(broken={"/dev/serial0"})
    with caplog.at_level(logging.ERROR):
        link, player = open_hardware(ONE_BOARD, serial_module=fake)
    assert isinstance(player, AudioPlayer) and not isinstance(player, Esp32AudioPlayer)   # the Pi's own speaker
    assert isinstance(link, NullEspLink) or isinstance(link, SerialEspLink) is False
    assert "could not open the shared ESP32 link" in caplog.text


def test_bad_baud_or_ack_setting_falls_back_to_defaults(caplog):
    fake = FakeSerialModule()
    with caplog.at_level(logging.ERROR):
        _, player = open_hardware({**ONE_BOARD, "AUDIO_BAUD": "fast", "AUDIO_ACK_BYTE": "x"}, serial_module=fake)
    assert fake.opened == [("/dev/serial0", 921600)] and player._ack == b"D"
    assert "bad AUDIO_BAUD" in caplog.text


def test_no_ports_configured_means_pi_speaker_and_no_motion():
    link, player = open_hardware({})
    assert isinstance(link, NullEspLink) and isinstance(player, AudioPlayer)


def test_locked_serial_transaction_blocks_other_writers_until_it_ends():
    import threading, time
    conn = FakeConn()
    shared = LockedSerial(conn)
    order = []
    with shared.transaction():
        t = threading.Thread(target=lambda: (shared.write(b"talk\n"), order.append("talk")))
        t.start()
        time.sleep(0.05)
        shared.write(b"audio-chunk")
        order.append("audio")
    t.join()
    assert order == ["audio", "talk"] and conn.written == [b"audio-chunk", b"talk\n"]


@pytest.mark.parametrize("ack,reply_stream,acked", [
    (b"\x06", [0x4F, 0x4B, 0x0A, 0x06], True),     # text reply 'OK\n' is skipped, then the 0x06 ack ends the wait
    (b"\x06", [0x44, 0x44], False),                # 'D' is NOT an ack when the one-board ack byte is configured
    (b"D", [0x06], False),                         # and 0x06 is not an ack for the S3-bridge setup
    (b"D", [0x44], True),
])
def test_player_only_accepts_its_configured_ack_byte(ack, reply_stream, acked, monkeypatch, caplog):
    import pi_app.audio_player as ap
    ticks = iter(range(0, 10000))
    monkeypatch.setattr(ap.time, "monotonic", lambda: next(ticks) * 0.1)    # advances 0.1 s per check: a missing ack still times out quickly
    conn = FakeConn(replies=reply_stream)
    player = Esp32AudioPlayer(conn=conn, ack=ack, bytes_per_second=4000, sleep=lambda s: None, clock=lambda: 0.0)
    with caplog.at_level(logging.WARNING):
        player.play(b"\0" * 500)
    assert ("never acknowledged" not in caplog.text) == acked
