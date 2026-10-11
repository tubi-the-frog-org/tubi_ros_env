"""Run one request through the real decision + TTS pipeline, with no
website and no ESP32 needed -- just real API keys. Prints the
resulting package and saves the audio (if any) to a file so you can
listen to it.

Usage (from inside pi_app/, with a venv that has requirements.txt
installed and LLM_API_KEY / TTS_API_KEY set in the environment):

    python3 -m tools.try_request --text "I would turn into an owl and fly around at night"
    python3 -m tools.try_request --json-file tools/sample_request.json
    python3 -m tools.try_request --text "..." --play

A --json-file's content is just {"text": "..."}.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from pi_app.decision_client import DecisionClient
from pi_app.decision_service import DecisionService
from pi_app.dotenv import load_secrets_env
from pi_app.request_budget import CharacterBudget, RequestBudget
from pi_app.tts_client import TtsClient


def main():
    load_secrets_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", help="the text to run through the pipeline")
    parser.add_argument("--json-file", help='a JSON file containing {"text": "..."}')
    parser.add_argument("--out", default="tools/output.mp3", help="where to save the resulting audio, if any")
    parser.add_argument("--play", action="store_true", help="also play the audio immediately")
    args = parser.parse_args()

    if not args.text and not args.json_file:
        parser.error("pass --text or --json-file")
    if args.text and args.json_file:
        parser.error("pass only one of --text or --json-file")

    if args.json_file:
        with open(args.json_file) as f:
            text = json.load(f)["text"]
    else:
        text = args.text

    missing = [name for name in ("LLM_API_KEY", "TTS_API_KEY") if not os.environ.get(name)]
    if missing:
        print(f"warning: {', '.join(missing)} not set in the environment -- the call(s) will fail", file=sys.stderr)

    decision_client = DecisionClient(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-5-nano"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    tts_client = TtsClient(
        base_url=os.environ.get("TTS_BASE_URL", "https://api.elevenlabs.io/v1"),
        model=os.environ.get("TTS_MODEL", "eleven_turbo_v2_5"),
        api_key=os.environ.get("TTS_API_KEY", ""),
        voice_id=os.environ.get("TTS_VOICE_ID", "pNInz6obpgDQGcFmaJgB"),
    )
    service = DecisionService(decision_client, tts_client, RequestBudget(), CharacterBudget())

    message = service.handle("try-request", text)

    summary = dataclasses.asdict(message)
    summary["audio_mp3"] = f"<{len(message.audio_mp3)} bytes>" if message.audio_mp3 else None
    print(json.dumps(summary, indent=2))

    if message.audio_mp3:
        with open(args.out, "wb") as f:
            f.write(message.audio_mp3)
        print(f"audio saved to {args.out}", file=sys.stderr)

        if args.play:
            from pi_app.audio_player import AudioPlayer
            AudioPlayer().play(message.audio_mp3)
    else:
        print("no audio was generated (see 'source' above)", file=sys.stderr)


if __name__ == "__main__":
    main()
