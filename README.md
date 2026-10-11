# Tubi the Wizard Frog

A voice-conversation animatronic. The website captures guest speech and sends
transcripts to the Pi service over WebSockets. The AI preserves Tubi's
personality and question-led conversation, then freely (even randomly) chooses
a fictional magic-user type and 1–10 level. Tubi gives a playful explanation
connected to something the guest said; there is no scoring or calculation.
Replies are spoken through ElevenLabs TTS. The ESP32
hardware and servo drivers remain in the repository; assessment does not
invoke the dispenser.

See [CLAUDE.md](CLAUDE.md) for the architecture, behavior, and hardware notes.

## Pi Development

```sh
cd pi_app
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest test/
```

Copy `pi_app/secrets.env.example` to `pi_app/secrets.env` and add the LLM and
TTS keys to make real API calls.

## Test Without the Website or ESP32

Run the multi-turn conversation using your microphone and local Whisper
transcription:

```sh
cd pi_app
.venv/bin/pip install -r tools/requirements-mic.txt
.venv/bin/python -m tools.talk_to_tubi
```

The final magic type and level are printed and saved to
`tools/magic_result.json`.

For a single AI assessment and TTS call:

```sh
cd pi_app
.venv/bin/python -m tools.try_request --text "I like quiet places and patient work"
```

## Run the Pi Service

```sh
cd pi_app
.venv/bin/python -m pi_app.teammate_client
```

The Pi service connects to the website's WebSocket hub, speaks each reply, and
sends the final `magic-assessment` display message. Configure the website URL,
API keys, and optional audio/ESP32 ports in `secrets.env`.

## Hardware

The ESP32 firmware and serial/audio drivers are retained. Servo movement code
and hardware test commands remain available, but the AI assessment does not
issue a dispense command.