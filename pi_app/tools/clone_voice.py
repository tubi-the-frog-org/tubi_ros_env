"""One-time setup: clone a custom voice on ElevenLabs from a local
audio sample, for Tubi's voice. This is NOT something the running
service does at any point -- run it once, then put the voice_id it
prints into secrets.env as TTS_VOICE_ID for tts_client.py to use.

Needs the elevenlabs SDK, which is NOT a runtime dependency of
tts_client.py (that talks to the REST API directly via plain
requests, no SDK needed there):
    .venv/bin/pip install -r tools/requirements-voice-clone.txt

Usage (from inside pi_app/, with TTS_API_KEY in secrets.env):
    .venv/bin/python -m tools.clone_voice
    .venv/bin/python -m tools.clone_voice --audio config/max.mp3 --name "Tubi"
"""

from __future__ import annotations

import argparse
import os
import sys

from pi_app.dotenv import load_secrets_env


def main():
    load_secrets_env()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", default="config/max.mp3", help="path to a local audio sample of the voice to clone")
    parser.add_argument("--name", default="Tubi", help="name for the cloned voice on ElevenLabs")
    args = parser.parse_args()

    api_key = os.environ.get("TTS_API_KEY", "")
    if not api_key:
        print("error: TTS_API_KEY not set (check secrets.env)", file=sys.stderr)
        raise SystemExit(1)

    if not os.path.exists(args.audio):
        print(f"error: audio sample not found: {args.audio}", file=sys.stderr)
        raise SystemExit(1)

    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        print(
            "error: elevenlabs SDK not installed -- run: "
            ".venv/bin/pip install -r tools/requirements-voice-clone.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client = ElevenLabs(api_key=api_key)

    print(f"cloning a voice named '{args.name}' from {args.audio}...", file=sys.stderr)
    with open(args.audio, "rb") as f:
        try:
            result = client.voices.ivc.create(name=args.name, files=[f])
        except Exception as e:  # noqa: BLE001 -- one-off CLI tool, surface whatever the SDK raises clearly
            print(f"error: voice cloning failed: {e}", file=sys.stderr)
            raise SystemExit(1)

    print(f"\nvoice created: {result.voice_id}")
    if result.requires_verification:
        print("note: ElevenLabs flagged this voice as requiring verification before use.", file=sys.stderr)
    print(f"\nAdd this to secrets.env:\n  TTS_VOICE_ID={result.voice_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
