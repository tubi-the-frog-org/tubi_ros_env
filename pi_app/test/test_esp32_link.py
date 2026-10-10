from pi_app.esp32_link import NullEspLink, SerialEspLink


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


def test_open_trap_door_sends_byte_2():
    link, fake = _link_and_fake()
    link.open_trap_door()
    assert fake.writes == [b"2"]


def test_close_trap_door_sends_byte_1():
    link, fake = _link_and_fake()
    link.close_trap_door()
    assert fake.writes == [b"1"]


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
