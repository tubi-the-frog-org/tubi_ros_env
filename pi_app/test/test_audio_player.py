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
