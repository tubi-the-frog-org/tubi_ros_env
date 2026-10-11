"""Is the Pi's GPIO serial port (pins 8/10 = /dev/serial0, the audio link) healthy?

Step 1 (Pi alone): connect a jumper wire from pin 8 (TXD) to pin 10 (RXD), then run
    .venv/bin/python -m tools.check_pi_uart            # loopback at the audio link's 921600 baud
A pass proves the Pi's port, its baud rate and the "serial console off / Bluetooth off" setup are
fine, so any remaining audio/ack problem is in the S3/DOIT wiring or firmware.

Step 2 (with the S3 attached, jumper removed): listen for the S3's ack path
    .venv/bin/python -m tools.check_pi_uart --listen 10
and play a line (tools.test_esp32_audio). Seeing "D" proves S3 GPIO5 -> Pi pin 10 works.
"""

import argparse
import os
import sys
import time

import serial


def loopback(port: str, baud: int, size: int = 4096) -> bool:
    payload = bytes(range(256)) * (size // 256)
    with serial.serial_for_url(port, baud, timeout=2.0) as s:
        s.reset_input_buffer()
        s.write(payload)
        s.flush()
        got = s.read(len(payload))
    ok = got == payload
    print(f"loopback {port} @ {baud}: sent {len(payload)} bytes, got {len(got)} back -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("  nothing back: jumper pin 8 to pin 10? serial console still enabled? dtoverlay=disable-bt set?")
        print("  garbled: wrong baud or a mini-UART in place of the PL011 (/dev/serial0 should point at ttyAMA0)")
    return ok


def listen(port: str, baud: int, seconds: float) -> int:
    seen = bytearray()
    deadline = time.monotonic() + seconds
    with serial.serial_for_url(port, baud, timeout=0.5) as s:
        s.reset_input_buffer()
        print(f"listening on {port} @ {baud} for {seconds:.0f}s ...")
        while time.monotonic() < deadline:
            seen += s.read(64)
    print(f"received {len(seen)} bytes: {bytes(seen)[:40]!r}")
    print("the S3's ack ('D') arrived -> its TX wire to Pi pin 10 works" if b"D" in seen else "no 'D' seen")
    return len(seen)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("AUDIO_SERIAL_PORT") or "/dev/serial0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--listen", type=float, default=0, help="seconds to listen for the S3's ack instead of looping back")
    args = ap.parse_args()
    if args.listen:
        listen(args.port, args.baud, args.listen)
    else:
        sys.exit(0 if loopback(args.port, args.baud) else 1)


if __name__ == "__main__":
    main()
