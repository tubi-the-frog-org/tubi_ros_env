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


def test_dispense_sends_dispense_line():
    link, fake = _link_and_fake()
    link.dispense(2)
    assert fake.writes == [b"dispense 2\n"]


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
    link.dispense(1)


class _FakePigpioPi:
    """Just enough of pigpio.pi() to check what SoftSerialEspLink asks it to do."""

    def __init__(self, busy_polls=0):
        self.calls = []
        self.serial_payloads = []
        self._busy = busy_polls

    def set_mode(self, gpio, mode):
        self.calls.append(("set_mode", gpio, mode))

    def write(self, gpio, level):
        self.calls.append(("write", gpio, level))

    def wave_tx_busy(self):
        if self._busy > 0:
            self._busy -= 1
            return True
        return False

    def wave_clear(self):
        self.calls.append(("wave_clear",))

    def wave_add_serial(self, gpio, baud, data):
        self.serial_payloads.append((gpio, baud, data))

    def wave_create(self):
        return 7

    def wave_send_once(self, wave_id):
        self.calls.append(("wave_send_once", wave_id))

    def stop(self):
        self.calls.append(("stop",))


def test_soft_serial_link_idles_high_and_sends_firmware_commands_on_one_gpio():
    from pi_app.esp32_link import SoftSerialEspLink

    pi = _FakePigpioPi()
    link = SoftSerialEspLink(gpio=18, pi=pi)
    link.talk([300, 250, 400])
    link.dispense(2)

    assert ("set_mode", 18, 1) in pi.calls and ("write", 18, 1) in pi.calls  # output, idle high
    assert pi.serial_payloads[0] == (18, 115200, b"\ntalk 300 250 400\n")  # leading \n resyncs the board's line parser
    assert pi.serial_payloads[1][2] == b"\ndispense 2\n"
    assert ("wave_send_once", 7) in pi.calls


def test_soft_serial_link_waits_for_the_previous_transmission_to_finish():
    from pi_app.esp32_link import SoftSerialEspLink

    sleeps = []
    pi = _FakePigpioPi(busy_polls=3)
    SoftSerialEspLink(gpio=18, pi=pi, sleep=sleeps.append).talk([200])
    assert len(sleeps) == 3  # polled until idle before clearing the wave


def test_open_motor_link_prefers_soft_serial_and_never_raises(monkeypatch):
    import pi_app.esp32_link as link_mod

    # Soft serial requested but pigpiod unreachable / pigpio missing -> no-op link, not a crash.
    assert isinstance(link_mod.open_motor_link({"ESP32_SOFT_TX_GPIO": "18"}), link_mod.NullEspLink)
    # Bad config values and unopenable USB ports also degrade to the no-op link.
    assert isinstance(link_mod.open_motor_link({"ESP32_SOFT_TX_GPIO": "notanumber"}), link_mod.NullEspLink)
    assert isinstance(link_mod.open_motor_link({"ESP32_SERIAL_PORT": "/dev/does-not-exist"}), link_mod.NullEspLink)
    assert isinstance(link_mod.open_motor_link({}), link_mod.NullEspLink)

    sentinel = object()
    monkeypatch.setattr(link_mod, "SoftSerialEspLink", lambda gpio: ("soft", gpio))
    monkeypatch.setattr(link_mod, "SerialEspLink", lambda port: ("usb", port))
    both = {"ESP32_SOFT_TX_GPIO": "18", "ESP32_SERIAL_PORT": "/dev/ttyUSB0"}
    assert link_mod.open_motor_link(both) == ("soft", 18)  # soft serial wins when both are set
    assert link_mod.open_motor_link({"ESP32_SERIAL_PORT": "/dev/ttyUSB0"}) == ("usb", "/dev/ttyUSB0")
