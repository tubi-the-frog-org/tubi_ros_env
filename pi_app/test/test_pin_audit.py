"""Regression tests for the hardware wiring rules (see tools/audit_pins.py)."""

from tools import audit_pins


def test_all_pins_and_links_are_consistent_across_the_firmwares_and_the_pi():
    assert audit_pins.audit(echo=False) == []


def test_audit_catches_a_servo_on_an_input_only_pin(monkeypatch):
    """The exact mistake that cost a debugging session: the arm on GPIO35 (input-only on the ESP32)."""
    real = audit_pins.src
    monkeypatch.setattr(audit_pins, "src", lambda p: real(p).replace("GPIO_NUM_22,", "GPIO_NUM_35,") if p.endswith("pwm.c") else real(p))
    failures = audit_pins.audit(echo=False)
    assert any("arm" in f and "OUTPUT" in f for f in failures), failures


def test_audit_catches_two_servos_sharing_a_pin(monkeypatch):
    real = audit_pins.src
    monkeypatch.setattr(audit_pins, "src", lambda p: real(p).replace("GPIO_NUM_21,", "GPIO_NUM_22,") if p.endswith("pwm.c") else real(p))
    assert any("distinct" in f for f in audit_pins.audit(echo=False))


def test_audit_catches_mismatched_link_baud_rates(monkeypatch):
    real = audit_pins.src
    monkeypatch.setattr(audit_pins, "src", lambda p: real(p).replace("#define PI_BAUD       921600", "#define PI_BAUD       115200") if "freenove" in p else real(p))
    assert any("baud" in f for f in audit_pins.audit(echo=False))


def test_audit_catches_a_wrong_tts_format_for_the_dac(monkeypatch):
    real = audit_pins.src
    monkeypatch.setattr(audit_pins, "src", lambda p: real(p).replace("#define DAC_FREQ_HZ    22050", "#define DAC_FREQ_HZ    44100") if "dac_music" in p else real(p))
    assert any("22050" in f for f in audit_pins.audit(echo=False))
