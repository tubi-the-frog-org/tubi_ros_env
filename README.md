# Wizard snack animatronic — Pi-side service

A voice-quiz animatronic, hackathon team project. A teammate's website captures
the guest's speech and sends the text over a WebSocket; this service
(`pi_app/`) picks a snack, writes and voices the reply via two cloud calls,
plays it out of a speaker on the Pi, and pulses a teammate's ESP32 over serial
to open a trap door. See `CLAUDE.md` for the full design and history
(including why it's not ROS 2 — that was tried first and dropped).

## Dev environment

Plain Python, no Docker/ROS needed:

```sh
cd pi_app
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest test/
```

## Testing without the website or the ESP32

Secrets load automatically from `pi_app/secrets.env` (copy `secrets.env.example`
and fill in real keys) — no need to `export` them by hand.

**Have the actual conversation, by voice, using your own mic:**

```sh
cd pi_app
.venv/bin/pip install -r tools/requirements-mic.txt   # one-time; not in requirements.txt
.venv/bin/python -m tools.talk_to_tubi
```

This is the real multi-turn flow (intro, questions, confirmation, reveal with
percentages) running entirely locally — speech-to-text is a local Whisper
model, not a cloud call, so only the decision+TTS calls cost anything. First
run will trigger a macOS microphone-permission prompt for your terminal app;
you'll need to approve that yourself. The final result is printed and saved to
`tools/quiz_result.json`.

**Or test just one question/answer pair (no mic, no conversation state):**

```sh
cd pi_app
.venv/bin/python -m tools.try_request --text "something crunchy and salty"
```

Prints the resulting package (snack id, slot, reply text, source) as JSON and
saves the generated audio to `tools/output.mp3` (pass `--play` to also play it
immediately). Use `--json-file some.json` with `{"text": "..."}` instead of
`--text` if you'd rather script a batch of inputs.

## Running it for real

```sh
cd pi_app
export LLM_API_KEY=...
export TTS_API_KEY=...
export WEBSITE_WS_URL=ws://localhost:3000   # the teammate's website
export ESP32_SERIAL_PORT=/dev/ttyUSB0       # omit to run with no ESP32 attached
.venv/bin/python -m pi_app.teammate_client
```

This connects out to the website's WebSocket as "the teammate system," drives
the real multi-turn conversation per guest session, and drives the
ESP32/speaker. It does not host the website or the ESP32 firmware — both are
teammates' code, in `website/` and `esp32_firmware/` respectively; see
`CLAUDE.md`'s History section for what's actually in each. The website's
current contract doesn't yet support multi-turn (see
`pi_app/docs/multi_turn_contract_proposal.md`) — use `tools/talk_to_tubi.py`
above to test the real conversation until that's built.

## Swapping snacks

Edit `pi_app/config/snacks.json` — the running service picks it up
automatically within a couple seconds (hot reload polls the file's mtime). If
the edited file is invalid JSON or fails schema validation, it logs the error
and keeps serving the last-known-good config rather than crashing. Note:
`slot` is a plain integer, not an angle — the ESP32 doesn't do per-slot
routing yet (see CLAUDE.md), so it only matters for whenever that gets built.

## Repo layout

- `pi_app/` — the Pi-side service (this task's scope).
- `esp32_firmware/` — a teammate's ESP32 firmware. Out of scope here.
- `website/` — a teammate's guest-facing website. Out of scope here.

## Risks and recommendations (living list, updated as the project grows)

- **Total network dependence.** There's no offline fallback by design — a
  dropped hotspot means the whole system is down, not degraded. See CLAUDE.md's
  History section for why.
- **Two cloud cost surfaces.** The decision call and the TTS call are both
  billed per-request/per-character against a publicly reachable endpoint.
  `RequestBudget`/`CharacterBudget` exist in `pi_app/request_budget.py` but
  their caps need real values set before this goes live, not left at defaults.
- **Key security.** `LLM_API_KEY` and `TTS_API_KEY` come from env/`secrets.env`
  only (see `pi_app/secrets.env.example`), never logged above DEBUG, never
  sent to or held by the browser — the browser only ever talks to the
  website's own server.
- **Prompt injection.** The guest's text reaches the decision LLM as untrusted
  input; the system prompt frames it as data, and the structured-output schema
  + reply validation (must be a real snack, must mention it, no URL,
  length-capped) bound what a successful injection could even produce.
- **No per-slot ESP32 routing yet.** The real firmware only opens/closes one
  trap door — it can't yet physically select a specific snack. Worth raising
  with the ESP32 teammate before relying on it for a real demo.
