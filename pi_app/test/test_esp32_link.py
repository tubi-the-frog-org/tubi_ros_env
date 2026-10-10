from pi_app.esp32_link import NullEspLink, SerialEspLink, word_durations_ms


class FakeSerial:
    """Stands in for pyserial's Serial -- just records what was written."""

    def __init__(self, port, baudrate, timeout=None):
        self.port = port
        self.baudrate = baudrate
        self.writes = []
        self.closed = False

    def write(self, data: bytes):
        self.writes.append(data)

    def close(self):
        self.closed = True


def _link_and_fake(port="/dev/esp32", baudrate=115200):
    fakes = []

    def factory(port, baudrate, timeout):
        fake = FakeSerial(port, baudrate, timeout)
        fakes.append(fake)
        return fake

    link = SerialEspLink(port, baudrate, serial_factory=factory)
    return link, fakes[0]


def test_open_trap_door_sends_door_open_line():
    link, fake = _link_and_fake()
    link.open_trap_door()
    assert fake.writes == [b"door open\n"]


def test_close_trap_door_sends_door_close_line():
    link, fake = _link_and_fake()
    link.close_trap_door()
    assert fake.writes == [b"door close\n"]


def test_set_slot_sends_bin_line():
    link, fake = _link_and_fake()
    link.set_slot(3)
    assert fake.writes == [b"bin 3\n"]


def test_talk_sends_word_durations():
    link, fake = _link_and_fake()
    link.talk([200, 350, 150])
    assert fake.writes == [b"talk 200 350 150\n"]


def test_talk_with_no_words_sends_nothing():
    link, fake = _link_and_fake()
    link.talk([])
    assert fake.writes == []


def test_word_durations_split_audio_by_word_length():
    ms = word_durations_ms("hi wonderful", 2.0)
    assert len(ms) == 2 and ms[1] > ms[0]
    assert abs(sum(ms) - 2000) <= 2


def test_word_durations_empty_text():
    assert word_durations_ms("", 2.0) == []


def test_uses_115200_baud_by_default():
    link, fake = _link_and_fake()
    assert fake.baudrate == 115200


def test_close_closes_underlying_connection():
    link, fake = _link_and_fake()
    link.close()
    assert fake.closed is True


def test_null_link_does_nothing():
    link = NullEspLink()
    link.open_trap_door()  # should not raise
    link.close_trap_door()  # should not raise
    link.set_slot(1)
    link.talk([100])
