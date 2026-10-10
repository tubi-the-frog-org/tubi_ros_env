import json

from pi_app.esp32_link import NullEspLink, SerialEspLink
from pi_app.message_package import SnackMessage


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


def test_sends_header_and_audio_bytes():
    link, fake = _link_and_fake()
    message = SnackMessage(request_id="req-1", snack_id="chips", reply_text="chips!", audio_mp3=b"mp3data", source="ok")

    link.send(message)

    assert len(fake.writes) == 2
    header = json.loads(fake.writes[0].decode("utf-8").strip())
    assert header == {"request_id": "req-1", "snack_id": "chips", "audio_len": 7}
    assert fake.writes[1] == b"mp3data"


def test_sends_header_only_when_no_audio():
    link, fake = _link_and_fake()
    message = SnackMessage(request_id="req-2", snack_id="none", reply_text="nothing", audio_mp3=None, source="ok_no_audio")

    link.send(message)

    assert len(fake.writes) == 1
    header = json.loads(fake.writes[0].decode("utf-8").strip())
    assert header["audio_len"] == 0


def test_close_closes_underlying_connection():
    link, fake = _link_and_fake()
    link.close()
    assert fake.closed is True


def test_null_link_does_nothing():
    link = NullEspLink()
    message = SnackMessage(request_id="req-3", snack_id="chips", reply_text="chips!", audio_mp3=b"x", source="ok")
    link.send(message)  # should not raise
