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
    audio = bytes(range(256)) * 80  # 20480 bytes

    player.play(audio)

    assert conn.written[:3] == b"AUD"
    assert int.from_bytes(conn.written[3:7], "little") == len(audio)
    assert conn.written[7:] == audio
    assert len(sleeps) == (len(audio) - 8192 + 1023) // 1024  # everything after the burst is paced


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
