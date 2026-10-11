# One ESP32 for audio + servos, wired straight to the Pi

Firmware: `esp32_firmware/src/tubi_one_board` (a new project; `motor_esp2`, `dac_music_decoder` and the S3
bridge are untouched and still build). Pi side: `pi_app/hardware.py`.

## Wiring (3.3 V logic, all grounds shared)

| Pi (BCM / header pin) | ESP32 (DOIT DevKit V1) | Function |
|---|---|---|
| GPIO14 TXD / pin 8 | GPIO16 | UART2 RX: audio + commands in, 921600 baud |
| GPIO15 RXD / pin 10 | GPIO17 | UART2 TX: replies + end-of-clip ack |
| GPIO18 / pin 12 | GPIO18 | UART2 RTS: the board drives it HIGH to say "pause". Pi side stays an INPUT |
| GND | GND | required |
| | GPIO25 | DAC out to the LM386 |
| | GPIO15 | head servo (strapping pin) |
| | GPIO2 | arm servo (strapping pin) |
| | GPIO21 / GPIO19 | lazy susan / trap door |

Servo supply ground must be common with the ESP32 and the Pi.

## One wire, two kinds of traffic
The Pi writes both to `/dev/serial0`; the firmware tells them apart (`link_demux.c`):
- audio: `AUD` + uint32 length (LE) + MP3 bytes (MP3 must be 22050 Hz: the Pi requests `mp3_22050_32`)
- text commands: `talk 300 250 ...`, `dispense 2`, `status`, ... one per line
After a clip finishes the board sends one ACK byte (0x06). Text replies come back on the same wire.

## secrets.env
```
AUDIO_SERIAL_PORT=/dev/serial0
ESP32_SERIAL_PORT=/dev/serial0
AUDIO_ACK_BYTE=6
```
Same device in both -> the Pi opens it once and shares it (writes are locked; the audio stream holds the lock so a
command can never land inside an audio frame).

Keep Pi GPIO18 an input: do not enable `audremap`, PWM or I2S overlays on pin 18.

## Bench testing without the Pi
Open `pio device monitor` (115200) on the board's USB port and type commands (`head up`, `arm down`,
`talk 300 300 300`, `dispense 2`, `tasks`). Replies appear in the monitor.

## Flashing
`cd esp32_firmware/src/tubi_one_board && pio run -t upload`. If upload fails with "Wrong boot mode", unplug the arm
wire (GPIO2), flash, plug it back.

## Tests
`pi_app/test/test_firmware_demux.py` compiles the real `link_demux.c` and (a) fuzzes it with random streams and
(b) drives the Pi's real classes into it over a fake wire. `python -m tools.audit_pins` checks every pin and link
setting across all firmwares. `python -m tools.check_pi_uart` tests the Pi's UART with a loopback jumper.
