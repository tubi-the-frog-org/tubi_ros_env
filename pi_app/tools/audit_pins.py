"""Cross-checks every pin and link setting in the three ESP32 firmwares and the Pi's audio player,
reading the real source files (read-only), so a pin edit that breaks a rule is caught by the tests.

    cd pi_app && .venv/bin/python -m tools.audit_pins

Chip facts come from ESP-IDF's soc_caps.h: ESP32 GPIO34-39 are input-only; GPIO6-11 are the SPI flash;
on the ESP32-S3 N8R8, GPIO26-37 are flash/octal PSRAM. Strapping pins are avoided on purpose.
"""
import re
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[2]


def src(p):
    return (R / p).read_text()


def audit(echo=True):

    motor=src("esp32_firmware/src/motor_esp2/src/pwm.c")
    doit_all=src("esp32_firmware/src/dac_music_decoder/src/main.c")
    doit=doit_all[doit_all.rindex("ESP32 DOIT DevKit V1 : UART2"):]   # the active copy (the file keeps a commented older copy on top)
    s3=src("esp32_firmware/src/freenove_32s3_sd/src/main.c")
    pi_audio=src("pi_app/pi_app/audio_player.py")

    def defs(text, names):
        out={}
        for n in names:
            m=re.findall(rf"#define\s+{n}\s+(\d+)", text)
            out[n]=int(m[-1]) if m else None
        return out

    servo={m[0]:int(m[1]) for m in re.findall(r'\.name = "(\w+)",\s+\.gpio = GPIO_NUM_(\d+)', motor)}
    d=defs(doit,["LINK_RX_GPIO","LINK_TX_GPIO","LINK_RTS_GPIO","LINK_BAUD","DAC_FREQ_HZ"])
    dac_ch0 = "DAC_CHANNEL_MASK_CH0" in doit
    s=defs(s3,["SD_CMD_GPIO","SD_CLK_GPIO","SD_D0_GPIO","PI_RX_GPIO","PI_TX_GPIO","PI_BAUD","LINK_TX_GPIO","LINK_RX_GPIO","LINK_CTS_GPIO","LINK_BAUD"])
    pi_baud=int(re.search(r"baudrate: int = (\d+)", pi_audio).group(1))

    # ---- chip capability tables (from ESP-IDF soc_caps.h) ----
    ESP32_VALID  = {g for g in range(40)} - {24,28,29,30,31}
    ESP32_OUT    = ESP32_VALID - {34,35,36,37,38,39}
    ESP32_FLASH  = {6,7,8,9,10,11}                 # connected to the SPI flash on WROOM-32
    ESP32_STRAP  = {0,2,5,12,15}
    S3_VALID     = {g for g in range(49)} - {22,23,24,25}
    S3_FLASH_PSRAM = set(range(26,33)) | {33,34,35,36,37}  # 26-32 flash; 33-37 octal PSRAM on the N8R8
    S3_STRAP     = {0,3,45,46}

    fails=[]; notes=[]
    out = print if echo else (lambda *a, **k: None)
    def check(ok,msg):
        if echo: out(("PASS " if ok else "FAIL ")+msg)
        if not ok: fails.append(msg)

    out("=== motor ESP32 (DOIT, pwm.c)  UART0=USB 115200")
    for name,g in servo.items():
        check(g in ESP32_OUT, f"servo {name:<5} GPIO{g}: valid OUTPUT pin")
        check(g not in ESP32_FLASH|ESP32_STRAP, f"servo {name:<5} GPIO{g}: not a flash/strapping pin")
    check(len(set(servo.values()))==len(servo)==4, f"4 servos on 4 distinct pins {servo}")
    check(not (set(servo.values()) & {1,3}), "servo pins avoid UART0 TX/RX (GPIO1/3, the USB link)")

    out("=== audio ESP32 (DOIT, dac_music_decoder)  UART2 + DAC")
    for k in ("LINK_TX_GPIO","LINK_RTS_GPIO"):
        check(d[k] in ESP32_OUT, f"{k}=GPIO{d[k]}: valid OUTPUT pin")
    check(d["LINK_RX_GPIO"] in ESP32_VALID, f"LINK_RX_GPIO=GPIO{d['LINK_RX_GPIO']}: valid input pin")
    used={d['LINK_RX_GPIO'],d['LINK_TX_GPIO'],d['LINK_RTS_GPIO'],25}
    check(not (used & (ESP32_FLASH|ESP32_STRAP)), f"audio pins {sorted(used)} avoid flash/strapping pins")
    check(dac_ch0 and 25 in ESP32_OUT, "DAC channel 0 = GPIO25 (valid output; GPIO26/DAC1 unused)")
    check(len(used)==4, "audio board pins all distinct (RX/TX/RTS/DAC)")

    out("=== S3 (Freenove N8R8, bridge)  UART2<-Pi, UART1->audio ESP32, SD card")
    s3_out=[s["PI_TX_GPIO"],s["LINK_TX_GPIO"],s["SD_CLK_GPIO"],s["SD_CMD_GPIO"],s["SD_D0_GPIO"]]
    s3_in=[s["PI_RX_GPIO"],s["LINK_RX_GPIO"],s["LINK_CTS_GPIO"]]
    allp=s3_out+s3_in
    check(all(g in S3_VALID for g in allp), f"all S3 pins {sorted(allp)} exist on the S3")
    check(not (set(allp)&S3_FLASH_PSRAM), "S3 pins avoid flash/octal-PSRAM pins (26-37)")
    check(not (set(allp)&S3_STRAP), "S3 pins avoid strapping pins (0,3,45,46)")
    check(len(set(allp))==len(allp), f"S3 pins all distinct {sorted(allp)}")
    notes.append("S3 GPIO4/GPIO5 (Pi link) are the camera SCCB pins on Freenove CAM boards: fine with no camera fitted.")

    out("=== cross-board wiring (each line must be one physical wire)")
    wires=[
     ("Pi pin8  GPIO14 TXD -> S3 PI_RX", f"GPIO4 (S3 PI_RX_GPIO={s['PI_RX_GPIO']})", s["PI_RX_GPIO"]==4),
     ("Pi pin10 GPIO15 RXD <- S3 PI_TX", f"GPIO5 (S3 PI_TX_GPIO={s['PI_TX_GPIO']})", s["PI_TX_GPIO"]==5),
    ]
    for a,b,ok in wires: check(ok, f"{a}: {b}")
    out(f"     S3 LINK_TX=GPIO{s['LINK_TX_GPIO']} -> audio LINK_RX=GPIO{d['LINK_RX_GPIO']}   (S3 TX -> DOIT RX)")
    out(f"     S3 LINK_RX=GPIO{s['LINK_RX_GPIO']} <- audio LINK_TX=GPIO{d['LINK_TX_GPIO']}   (DOIT TX -> S3 RX, carries the 'D' ack)")
    out(f"     S3 LINK_CTS=GPIO{s['LINK_CTS_GPIO']} <- audio LINK_RTS=GPIO{d['LINK_RTS_GPIO']} (flow control)")
    check(s["LINK_TX_GPIO"]==1 and d["LINK_RX_GPIO"]==16, "S3 GPIO1 TX -> DOIT GPIO16 RX matches the wiring comments")
    check(s["LINK_RX_GPIO"]==2 and d["LINK_TX_GPIO"]==17, "S3 GPIO2 RX <- DOIT GPIO17 TX matches the wiring comments")
    check(s["LINK_CTS_GPIO"]==14 and d["LINK_RTS_GPIO"]==18, "S3 GPIO14 CTS <- DOIT GPIO18 RTS matches the wiring comments")

    out("=== baud rates + audio format agree across the chain")
    check(pi_baud==s["PI_BAUD"], f"Pi Esp32AudioPlayer baud {pi_baud} == S3 PI_BAUD {s['PI_BAUD']}")
    check(s["LINK_BAUD"]==d["LINK_BAUD"], f"S3 LINK_BAUD {s['LINK_BAUD']} == audio LINK_BAUD {d['LINK_BAUD']}")
    check(d["DAC_FREQ_HZ"]==22050, "DAC runs at 22050 Hz == the mp3_22050_32 TTS format the Pi requests")
    check("mp3_22050_32" in src("pi_app/pi_app/teammate_client.py"), "Pi requests mp3_22050_32 whenever AUDIO_SERIAL_PORT is set")

    out("=== one-board firmware (tubi_one_board): audio + servos on ONE DOIT, wired straight to the Pi")
    ob_main = src("esp32_firmware/src/tubi_one_board/src/main.c")
    ob_servos = src("esp32_firmware/src/tubi_one_board/src/servos.c")
    ob = defs(ob_main, ["LINK_RX_GPIO","LINK_TX_GPIO","LINK_RTS_GPIO","LINK_BAUD","DAC_FREQ_HZ"])
    ob_servo = {m[0]: int(m[1]) for m in re.findall(r'\.name = "(\w+)",\s+\.gpio = GPIO_NUM_(\d+)', ob_servos)}
    ob_ack = int(re.search(r"#define ACK_BYTE\s+(0x[0-9a-fA-F]+|\d+)", ob_main).group(1), 0)
    ob_pins = [ob["LINK_RX_GPIO"], ob["LINK_TX_GPIO"], ob["LINK_RTS_GPIO"], 25, *ob_servo.values()]
    check(ob_servo == {"head": 15, "arm": 2, "lazy": 21, "door": 19}, f"servo pins are head=15 arm=2 lazy=21 door=19 (got {ob_servo})")
    check((ob["LINK_RX_GPIO"], ob["LINK_TX_GPIO"], ob["LINK_RTS_GPIO"]) == (16, 17, 18),
          "Pi link: ESP32 GPIO16 RX <- Pi GPIO14 TXD, GPIO17 TX -> Pi GPIO15 RXD, GPIO18 RTS -> Pi GPIO18")
    check("DAC_CHANNEL_MASK_CH0" in ob_main, "DAC channel 0 = GPIO25")
    check(len(set(ob_pins)) == len(ob_pins), f"all {len(ob_pins)} used pins distinct: {sorted(ob_pins)}")
    check(all(g in ESP32_OUT for g in [ob["LINK_TX_GPIO"], ob["LINK_RTS_GPIO"], 25, *ob_servo.values()]) and ob["LINK_RX_GPIO"] in ESP32_VALID,
          "every output is on an output-capable pin; RX is on a valid input")
    check(not (set(ob_pins) & ESP32_FLASH), "no pin on the SPI-flash lines")
    check(not (set(ob_pins) & ({0, 5, 12} | {1, 3})), "no pin on strapping GPIO0/5/12 or the USB console UART0 (GPIO1/3)")
    strap_used = sorted(set(ob_pins) & STRAP.keys()) if False else sorted(set(ob_pins) & ESP32_STRAP)
    check(set(strap_used) <= {2, 15}, f"strapping pins in use are only the two chosen on purpose (2 and 15): {strap_used}")
    notes.append("GPIO2 (arm) and GPIO15 (head) are boot-strapping pins: fine for a servo signal; if flashing fails with "
                 "'Wrong boot mode', unplug the arm wire (GPIO2) and retry.")
    check(ob["LINK_BAUD"] == pi_baud, f"one-board LINK_BAUD {ob['LINK_BAUD']} == the Pi's Esp32AudioPlayer baud {pi_baud}")
    check(ob["DAC_FREQ_HZ"] == 22050, "one-board DAC rate is 22050 Hz (mp3_22050_32)")
    check(ob_ack == 6 and "AUDIO_ACK_BYTE=6" in src("pi_app/pi_app/hardware.py"),
          f"firmware ack byte is 0x{ob_ack:02x} and the Pi's one-board config documents AUDIO_ACK_BYTE=6")
    ob_cmds = set(re.findall(r'strcmp\(cmd, "([a-z]+)"\)', ob_servos))
    pi_cmds = set(re.findall(r'_send\(f?"([a-z]+)', src("pi_app/pi_app/esp32_link.py")))
    check(pi_cmds <= ob_cmds, f"every command the Pi can send is understood by the firmware: {sorted(pi_cmds)} <= {sorted(ob_cmds)}")

    out("=== old layouts still consistent with each other: motor-only + audio-only pins would not collide on one DOIT")
    merged = set(servo.values()) | used
    check(len(merged)==len(servo)+len(used), f"motor+audio pins would not collide: {sorted(merged)}")

    out()
    for n in notes: out("NOTE", n)


    return fails


def out(*a):
    pass


if __name__ == "__main__":
    failed = audit(echo=True)
    print("\nALL PIN CHECKS PASSED" if not failed else f"\n{len(failed)} FAILED")
    sys.exit(1 if failed else 0)
