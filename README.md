# Wizard snack animatronic — Pi-side service

A voice-quiz animatronic: a guest speaks into a webpage, a cloud LLM picks a
snack and writes the spoken reply, a second cloud call turns that into audio,
and the Pi sends both to an ESP32 over serial. See `CLAUDE.md` for the full
design (including why it's not ROS 2 — that was tried first and dropped).

## Dev environment

Plain Python, no Docker/ROS needed:

```sh
cd pi_app
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest test/
```

## Running it

```sh
cd pi_app
export LLM_API_KEY=...        # decision calls
export TTS_API_KEY=...        # audio calls
export WEB_BRIDGE_TOKEN=...   # bearer token /speak requires
.venv/bin/python -m pi_app.server
```

`POST /speak {"text": "..."}` with header `Authorization: Bearer <WEB_BRIDGE_TOKEN>`
returns `{request_id, snack_id, text, audio_available, source}`. `GET /healthz`
for a liveness check.

## Swapping snacks

Edit `pi_app/config/snacks.json` — the running server picks it up automatically
within a couple seconds (hot reload polls the file's mtime). If the edited file
is invalid JSON or fails schema validation, the server logs the error and keeps
serving the last-known-good config rather than crashing.

## Repo layout

- `pi_app/` — the Pi-side service (this task's scope).
- `esp32_firmware/` — PlatformIO project for the ESP32 (servos, later audio
  playback). Out of scope here; see `esp32_firmware/README.md`.

## Risks and recommendations (living list, updated as the project grows)

- **Total network dependence.** There's no offline fallback by design — a
  dropped hotspot means the whole system is down, not degraded. See CLAUDE.md's
  History section for why.
- **Two cloud cost surfaces.** The decision call and the TTS call are both
  billed per-request/per-character against a publicly reachable endpoint.
  `RequestBudget`/`CharacterBudget` exist in `pi_app/request_budget.py` but
  their caps need real values set before this goes live, not left at defaults.
- **Key security.** `LLM_API_KEY`, `TTS_API_KEY`, `WEB_BRIDGE_TOKEN` come from
  env/`secrets.env` only (see `pi_app/secrets.env.example`), never logged above
  DEBUG, never sent to or held by the browser.
- **Prompt injection.** The guest's text reaches the decision LLM as untrusted
  input; the system prompt frames it as data, and the structured-output schema
  + reply validation (must be a real snack, must mention it, no URL,
  length-capped) bound what a successful injection could even produce.
