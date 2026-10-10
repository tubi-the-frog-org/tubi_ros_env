"""Have the actual multi-turn conversation with Tubi, using this
Mac's own microphone -- no website, no ESP32, no Pi needed.

Speech-to-text is local (openai-whisper, matching the approach
website/python/stt_worker.py already uses) rather than a cloud call,
so testing doesn't spend API credits on transcription, only on the
decision + TTS calls that are the actual thing being tested.

First run on macOS will trigger a system microphone-permission prompt
for the terminal app -- that's an OS dialog only you can approve, this
script can't click through it for you.

Setup (one-time, in addition to requirements-dev.txt):
    .venv/bin/pip install -r tools/requirements-mic.txt

Usage (from inside pi_app/, with LLM_API_KEY/TTS_API_KEY in secrets.env):
    .venv/bin/python -m tools.talk_to_tubi
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import uuid

from pi_app.audio_player import AudioPlayer
from pi_app.conversation_session import ConversationSession
from pi_app.decision_client import DecisionClient
from pi_app.dotenv import load_secrets_env
from pi_app.snacks import SnacksStore
from pi_app.tts_client import TtsClient


def _listen_and_transcribe(recognizer, microphone, whisper_model) -> str:
    import speech_recognition as sr

    print("listening... (speak now, pauses when you stop talking)", file=sys.stderr)
    with microphone as source:
        try:
            audio = recognizer.listen(source, timeout=10, phrase_time_limit=20)
        except sr.WaitTimeoutError:
            return ""

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(audio.get_wav_data())
        f.flush()
        result = whisper_model.transcribe(f.name, fp16=False)
        return (result.get("text") or "").strip()


def main():
    load_secrets_env()

    try:
        import speech_recognition as sr
        import whisper
    except ImportError:
        print("missing mic/STT deps -- run: .venv/bin/pip install -r tools/requirements-mic.txt", file=sys.stderr)
        raise SystemExit(1)

    missing = [name for name in ("LLM_API_KEY", "TTS_API_KEY") if not os.environ.get(name)]
    if missing:
        print(f"warning: {', '.join(missing)} not set -- calls will fail", file=sys.stderr)

    print("loading local whisper model (one-time, a few seconds)...", file=sys.stderr)
    whisper_model = whisper.load_model(os.environ.get("WHISPER_MODEL", "base"))

    recognizer = sr.Recognizer()
    microphone = sr.Microphone()
    print("calibrating for ambient noise, stay quiet for a moment...", file=sys.stderr)
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source)

    snacks_store = SnacksStore(os.environ.get("SNACKS_CONFIG_PATH", "config/snacks.json"))
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
    audio_player = AudioPlayer()

    session_id = str(uuid.uuid4())
    session = ConversationSession(session_id, decision_client, snacks_store.current)

    def speak(text: str):
        print(f"\nTubi: {text}\n")
        try:
            audio_player.play(tts_client.synthesize(text))
        except Exception as e:  # noqa: BLE001 -- a TTS hiccup shouldn't kill the conversation
            print(f"(TTS failed, continuing text-only: {e})", file=sys.stderr)

    speak(session.start())

    while not session.done:
        text = _listen_and_transcribe(recognizer, microphone, whisper_model)
        if not text:
            print("(didn't catch anything, try again)", file=sys.stderr)
            continue
        print(f"You: {text}")
        reply = session.respond(text)
        speak(reply)

    result = dataclasses.asdict(session.result)
    print(json.dumps(result, indent=2))

    out_path = os.environ.get("QUIZ_RESULT_PATH", "tools/quiz_result.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nresult saved to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
