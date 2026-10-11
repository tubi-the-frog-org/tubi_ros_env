"""Is the Pi's GPIO serial port (pins 8/10 = /dev/serial0, the audio link) healthy?

Step 1 (Pi alone): connect a jumper wire from pin 8 (TXD) to pin 10 (RXD), then run
    .venv/bin/python -m tools.check_pi_uart            # loopback at the audio link's 921600 baud
    .venv/bin/python -m tools.check_pi_uart --sweep    # 115200 / 230400 / 460800 / 921600, to see where loss begins
A pass proves the Pi's port, its baud rate and the "serial console off / Bluetooth off" setup are
fine, so any remaining audio/ack problem is in the S3/DOIT wiring or firmware.

Step 2 (with the S3 attached, jumper removed): listen for the S3's ack path
    .venv/bin/python -m tools.check_pi_uart --listen 10
and play a line (tools.test_esp32_audio). Seeing "D" proves S3 GPIO5 -> Pi pin 10 works.
"""

import argparse
import os
import sys
import threading
import time

import serial


def loopback(port: str, baud: int, size: int = 4096, chunk: int = 128, settle: float = 1.0, quiet: bool = False) -> bool:
    """Writes `size` bytes in `chunk`-byte pieces while a second thread reads continuously (full duplex,
    like real traffic) and compares what came back byte for byte."""
    payload = bytes((i * 7 + 3) % 256 for i in range(size))
    got = bytearray()
    stop = threading.Event()
    with serial.serial_for_url(port, baud, timeout=0.2) as s:
        s.reset_input_buffer()

        def reader():
            while not stop.is_set():
                data = s.read(512)
                if data:
                    got.extend(data)
                else:
                    time.sleep(0.002)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(0, size, chunk):
            s.write(payload[i:i + chunk])
        s.flush()
        time.sleep(settle)          # let the tail of the data come round
        stop.set()
        t.join()
    got = bytes(got)
    ok = got == payload
    if not quiet:
        bad = next((i for i, (a, b) in enumerate(zip(got, payload)) if a != b), None)
        print(f"loopback {port} @ {baud}: sent {len(payload)}, got {len(got)} back"
              f"{'' if bad is None else f', first wrong byte at {bad}'} -> {'PASS' if ok else 'FAIL'}")
        if not ok and not got:
            print("  nothing back: jumper pin 8 to pin 10? serial console still enabled? dtoverlay=disable-bt set?")
        elif not ok:
            print("  some bytes lost or changed: see the baud sweep (--sweep) to find where it starts")
    return ok


def sweep(port: str) -> None:
    print(f"loopback sweep on {port} (jumper pin 8 to pin 10; the ESP32 wire off pin 10):")
    for baud in (115200, 230400, 460800, 921600):
        loopback(port, baud, size=8192, quiet=False)


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
    ap.add_argument("--sweep", action="store_true", help="loopback at 115200, 230400, 460800 and 921600")
    ap.add_argument("--listen", type=float, default=0, help="seconds to listen for the S3's ack instead of looping back")
    args = ap.parse_args()
    if args.sweep:
        sweep(args.port)
    elif args.listen:
        listen(args.port, args.baud, args.listen)
    else:
        sys.exit(0 if loopback(args.port, args.baud) else 1)


if __name__ == "__main__":
    main()
