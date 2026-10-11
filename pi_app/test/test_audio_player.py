from pi_app.audio_player import AudioPlayer


def test_play_empty_audio_is_a_noop():
    player = AudioPlayer(player_cmd=["false"])  # would fail if actually invoked
    player.play(b"")
    player.play(None)


def test_play_with_no_detected_player_does_not_raise():
    player = AudioPlayer(player_cmd=None)
    player._player_cmd = None  # force the "nothing found" path regardless of this machine's tools
    player.play(b"some bytes")


def test_play_success_does_not_raise():
    player = AudioPlayer(player_cmd=["true"])  # always exits 0, ignores its args
    player.play(b"some mp3 bytes")


def test_play_failure_is_swallowed_not_raised():
    player = AudioPlayer(player_cmd=["false"])  # always exits 1
    player.play(b"some mp3 bytes")  # should log a warning, not raise


class _FakeSerial:
    def __init__(self, reply=b"D"):
        self.written = b""
        self._reply = reply

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += data

    def read(self, n):
        return self._reply


def test_esp32_player_frames_and_paces_the_track():
    from pi_app.audio_player import Esp32AudioPlayer

    conn, sleeps = _FakeSerial(), []
    player = Esp32AudioPlayer("x", serial_factory=lambda *a, **k: conn, sleep=sleeps.append)
    audio = bytes(range(256)) * 160  # 40960 bytes

    player.play(audio)

    assert conn.written[:3] == b"AUD"
    assert int.from_bytes(conn.written[3:7], "little") == len(audio)
    assert conn.written[7:] == audio
    assert sleeps and all(s > 0 for s in sleeps)  # everything after the burst is paced


def test_esp32_player_pacing_does_not_accumulate_scheduler_delay():
    """Regression for DAC dropouts on a busy Pi: every sleep overshoots by 50 ms, yet the track must
    still finish on the original schedule because pacing follows absolute deadlines."""
    from pi_app.audio_player import Esp32AudioPlayer

    now = [0.0]
    def sleep(d):
        now[0] += d + 0.050  # an overloaded scheduler wakes us late every time
    conn = _FakeSerial()
    player = Esp32AudioPlayer("x", bytes_per_second=4000, serial_factory=lambda *a, **k: conn,
                              sleep=sleep, clock=lambda: now[0])
    audio = b"\0" * 60000
    player.play(audio)

    paced_bytes = len(audio) - player.BURST_BYTES
    ideal = paced_bytes / (4000 * player.PACE_FACTOR)
    assert now[0] < ideal + 0.2, f"finished {now[0]:.2f}s vs ideal {ideal:.2f}s: delay is accumulating"


def test_esp32_player_burst_stays_well_inside_the_32kb_rings():
    from pi_app.audio_player import Esp32AudioPlayer

    assert Esp32AudioPlayer.BURST_BYTES <= 20 * 1024  # the S3 and the DOIT each buffer 32 KB with no flow control from the Pi


def test_esp32_player_survives_missing_ack(monkeypatch):
    import pi_app.audio_player as ap

    ticks = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(ap.time, "monotonic", lambda: next(ticks))
    player = ap.Esp32AudioPlayer("x", serial_factory=lambda *a, **k: _FakeSerial(reply=b""), sleep=lambda s: None)
    player.play(b"abc")  # logs a warning, must not raise or hang


def test_tts_bytes_per_second_follows_output_format():
    from pi_app.tts_client import TtsClient

    assert TtsClient("u", "m", "k", output_format="mp3_22050_32").bytes_per_second == 4000
    assert TtsClient("u", "m", "k").bytes_per_second == 16000


def test_uart_loopback_tool_passes_on_a_perfect_loop_and_fails_on_a_dead_one(monkeypatch):
    import tools.check_pi_uart as tool

    assert tool.loopback("loop://", 921600) is True

    class DeadLine:  # TX and RX not jumpered: everything written vanishes
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def reset_input_buffer(self): pass
        def write(self, b): pass
        def flush(self): pass
        def read(self, n): return b""

    monkeypatch.setattr(tool.serial, "serial_for_url", lambda *a, **k: DeadLine())
    assert tool.loopback("/dev/serial0", 921600) is False
