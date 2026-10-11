"""Speaker check with no website/LLM: TTS one line, stream it to the ESP32 audio firmware.

    cd pi_app && set -a && . ./secrets.env && set +a
    .venv/bin/python -m tools.test_esp32_audio "Ribbit! Testing, one two three."
"""

import os
import sys

from pi_app.dotenv import load_secrets_env
from pi_app.hardware import open_hardware
from pi_app.tts_client import TtsClient


def main():
    load_secrets_env()
    text = " ".join(sys.argv[1:]) or "Ribbit! Testing, one two three."
    tts = TtsClient(
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
        output_format="mp3_22050_32",
    )
    audio = tts.synthesize(text)
    print(f"got {len(audio)} bytes of mp3; streaming to {os.environ.get('AUDIO_SERIAL_PORT') or 'the Pi speaker'} ...")
    player = open_hardware(os.environ, tts.bytes_per_second)[1]  # honours AUDIO_SERIAL_PORT / AUDIO_ACK_BYTE
    player.play(audio)
    print("done (a warning above means the ESP32 never sent its end-of-clip ack)")


if __name__ == "__main__":
    main()
