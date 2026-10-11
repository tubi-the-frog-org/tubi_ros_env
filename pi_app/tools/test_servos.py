"""Head/arm check with no website or LLM: say one line, with the servos moving to it.

Does exactly what the brain does for a spoken line: synthesize speech, send the
per-word `talk` schedule to the motor ESP32 (ESP32_SERIAL_PORT), and stream the
mp3 to the audio path (AUDIO_SERIAL_PORT). Stop the brain first so it isn't also
holding the serial port:  sudo systemctl stop tubi-brain

    cd pi_app && set -a && . ./secrets.env && set +a
    .venv/bin/python -m tools.test_servos "Ribbit! Watch my head and arm move."

    --silent   servos only: no TTS, no audio (12 words, 350 ms each, or --words/--word-ms)
    --stop     send `stop` (head/arm to rest, door closed) and exit
"""

import argparse
import os
import sys
import time

from pi_app.audio_player import AudioPlayer, Esp32AudioPlayer
from pi_app.dotenv import load_secrets_env
from pi_app.esp32_link import SerialEspLink, estimate_audio_seconds, word_durations_ms
from pi_app.tts_client import TtsClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", help="what Tubi says")
    ap.add_argument("--silent", action="store_true", help="servos only, no speech")
    ap.add_argument("--words", type=int, default=12)
    ap.add_argument("--word-ms", type=int, default=350)
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()

    load_secrets_env()
    motor_port = os.environ.get("ESP32_SERIAL_PORT")
    if not motor_port:
        sys.exit("ESP32_SERIAL_PORT is not set (the motor ESP32's USB port, e.g. /dev/ttyUSB0). Check `ls /dev/ttyUSB* /dev/ttyACM*`.")

    print(f"opening motor ESP32 on {motor_port} (it resets when the port opens; waiting 2 s)...")
    link = SerialEspLink(motor_port)

    if args.stop:
        link._send("stop")
        print("sent: stop")
        return

    if args.silent:
        word_ms = [args.word_ms] * args.words
        print(f"talk x{len(word_ms)} words, {args.word_ms} ms each (~{sum(word_ms) / 1000:.1f} s), no audio")
        link.talk(word_ms)
        time.sleep(sum(word_ms) / 1000 + 1)
        print("done")
        return

    text = " ".join(args.text) or "Ribbit! Watch my head and arm move while I talk."
    audio_port = os.environ.get("AUDIO_SERIAL_PORT")
    tts = TtsClient(
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
        output_format=os.environ.get("TTS_OUTPUT_FORMAT") or ("mp3_22050_32" if audio_port else None),
    )
    audio = tts.synthesize(text)
    seconds = estimate_audio_seconds(audio, tts.bytes_per_second)
    word_ms = word_durations_ms(text, seconds)
    player = Esp32AudioPlayer(audio_port, bytes_per_second=tts.bytes_per_second) if audio_port else AudioPlayer()

    print(f"{len(audio)} bytes of mp3 (~{seconds:.1f} s), {len(word_ms)} words -> talk + audio")
    link.talk(word_ms)  # the ESP32 animates by itself from this schedule
    player.play(audio)
    time.sleep(1)
    print("done")


if __name__ == "__main__":
    main()
