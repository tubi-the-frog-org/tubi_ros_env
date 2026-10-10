# ESP32 firmware (PlatformIO)

Out of scope for the Pi-side ROS 2 task in `CLAUDE.md` — kept here so the
firmware lives in the same repo as the ROS 2 side it talks to. A separate
session implements this, against `docs/esp32_protocol.md` (written by the
`animatronic_esp32_bridge` work) for the serial JSON protocol, servo
mapping, watchdog timing, and later chunked audio transfer.

## Setup
Requires the [PlatformIO core CLI](https://platformio.org/install/cli) or
the PlatformIO VS Code extension (not installed on this Mac yet).

```sh
cd esp32_firmware
pio run              # build
pio run -t upload    # flash
pio device monitor   # serial monitor
```

Board is set to a placeholder `esp32dev` in `platformio.ini` — update it to
match the actual board once chosen.
