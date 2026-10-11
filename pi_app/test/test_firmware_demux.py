"""Tests that run the REAL firmware parser (esp32_firmware/src/tubi_one_board/src/link_demux.c), compiled on this
machine, against the Pi's real code. Skipped when there is no C compiler (e.g. a bare Pi image)."""

import ctypes
import random
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from pi_app.hardware import open_hardware

REPO = Path(__file__).resolve().parents[2]
FW_INCLUDE = REPO / "esp32_firmware/src/tubi_one_board/include"
FW_DEMUX = REPO / "esp32_firmware/src/tubi_one_board/src/link_demux.c"
HERE = Path(__file__).resolve().parent / "firmware"

pytestmark = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler available")


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    out = tmp_path_factory.mktemp("fw")
    fuzz, lib = out / "demux_fuzz", out / "libdemux_shim.so"
    common = ["cc", "-O2", "-Wno-deprecated-declarations", f"-I{FW_INCLUDE}"]
    subprocess.run(common + [str(HERE / "demux_fuzz.c"), str(FW_DEMUX), "-o", str(fuzz)], check=True)
    subprocess.run(common + ["-shared", "-fPIC", str(HERE / "demux_shim.c"), str(FW_DEMUX), "-o", str(lib)], check=True)
    return fuzz, lib


def test_firmware_demux_survives_random_streams_fed_in_random_chunks(build):
    """Commands, audio tracks full of 'AUD'/newlines, line noise, bogus headers, over-long lines, refused tracks."""
    fuzz, _ = build
    result = subprocess.run([str(fuzz), "3000"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-1500:]


class _Wire:
    """pyserial-like object whose far end is the real firmware demux."""

    def __init__(self, lib, chunk_seed=1):
        self.lib = lib
        lib.shim_new.restype = ctypes.c_void_p
        lib.shim_log.restype = ctypes.c_char_p
        lib.shim_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        self.d = lib.shim_new()
        self.rng = random.Random(chunk_seed)
        self.pending_reads = []          # bytes the "board" will send back
        self.fed = bytearray()

    def write(self, data):
        self.fed += data
        i = 0
        while i < len(data):              # a real UART delivers arbitrary fragments
            n = self.rng.randint(1, 300)
            self.lib.shim_feed(self.d, bytes(data[i:i + n]), len(data[i:i + n]))
            i += n
        # the board answers: a text reply, and the ack once a whole track has arrived
        log = self.lib.shim_log().decode()
        if log.count("E:ok|") > getattr(self, "acked", 0):
            self.acked = log.count("E:ok|")
            self.pending_reads += [b"O", b"K", b"\n", b"\x06"]   # reply text first, then the ack byte
        return len(data)

    def read(self, n=1):
        return self.pending_reads.pop(0) if self.pending_reads else b""

    def reset_input_buffer(self):
        self.pending_reads.clear()

    def flush(self): pass
    def close(self): pass

    def log(self):
        return self.lib.shim_log().decode()


class _SerialModule:
    def __init__(self, wire): self.wire = wire
    def Serial(self, port, baud, timeout=1.0): return self.wire


def _fnv(data):
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return f"{h:08x}"


def _hardware(wire):
    env = {"AUDIO_SERIAL_PORT": "/dev/serial0", "ESP32_SERIAL_PORT": "/dev/serial0", "AUDIO_ACK_BYTE": "6"}
    return open_hardware(env, bytes_per_second=400000, serial_module=_SerialModule(wire))  # fast pacing for the test


def test_pi_talk_audio_dispense_reach_the_real_firmware_parser_exactly(build):
    lib = ctypes.CDLL(str(build[1]))
    wire = _Wire(lib)
    link, player = _hardware(wire)
    rng = random.Random(7)
    audio = bytes(rng.randrange(256) for _ in range(40000))            # random bytes: 'AUD' and '\n' will occur
    audio = audio[:5000] + b"AUD\x10\x00\x00\x00" + b"\n" + audio[5000:]  # and a fake header inside the payload

    link.talk([300, 250, 400])
    player.play(audio)                  # returns only after the 0x06 ack, skipping the text reply before it
    link.dispense(2)

    assert wire.log() == f"L:talk 300 250 400|T:{len(audio)}|A:{_fnv(audio)}|E:ok|L:dispense 2|"


def test_a_motor_command_from_another_thread_never_lands_inside_the_audio_frame(build):
    lib = ctypes.CDLL(str(build[1]))
    wire = _Wire(lib, chunk_seed=3)
    link, player = _hardware(wire)
    audio = bytes(random.Random(11).randrange(256) for _ in range(60000))

    t = threading.Thread(target=lambda: [link.talk([200, 200]) for _ in range(20)])
    t.start()
    player.play(audio)
    t.join()

    log = wire.log()
    assert f"T:{len(audio)}|A:{_fnv(audio)}|E:ok|" in log               # the track arrived byte-exact
    assert log.count("L:talk 200 200|") == 20 and "E:bad" not in log     # and every command arrived whole
