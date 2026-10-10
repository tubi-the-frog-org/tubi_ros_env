# Project: Wizard snack animatronic — Pi-side service

## What we're building
A voice-quiz animatronic. The Pi hosts the guest-facing webpage itself (mic capture + browser speech-to-text).
The transcribed text goes to a cloud LLM that picks a snack AND writes the spoken reply in one call; a second
cloud call turns that reply into an mp3. The Pi sends `{snack_id, audio_mp3}` to an ESP32 over serial. The ESP32
owns the physical slot/angle mapping, drives the servos, and (later) plays the audio. This task covers ONLY the
Pi-side service. Do not write ESP32 firmware or the guest-facing frontend (mic capture page).

## History (why the design looks like this)
Three real pivots happened while building this, in order — kept here so a reader isn't confused by
code/decisions that look like they contradict each other:
1. **Started as a ROS 2 workspace** with a local Ollama LLM (`qwen2.5:0.5b`) as the "brain". Measured on the real
   Pi: ~2-3 tok/s generation, 8-40 s per call, and the model could not reliably reason "crunchy" -> a snack or
   write coherent prose. Memory was dominated by Ollama (~600 MB of 905 MiB total).
2. **Replaced Ollama with a cloud LLM** (OpenAI-compatible, structured output) as the primary decision-maker,
   keeping Ollama as an optional off-by-default `local` fallback provider alongside `keyword` and `fallback`,
   inside a ROS 2 node.
3. **Dropped ROS 2 and the offline/keyword/local path entirely.** The whole pipeline is a linear
   request -> cloud call(s) -> serial write — ROS 2's DDS discovery and per-node overhead wasn't earning its
   memory cost for that, and offline resilience was explicitly deprioritized in favor of the cloud LLM deciding
   everything, including writing the spoken reply itself (no more pre-written lines in the normal path). This is
   the current design. `esp32_firmware/` (PlatformIO, still just a stub) is unaffected by any of this.

## Measured facts about the Pi (do not assume otherwise)
- Raspberry Pi 3, 905 MiB RAM, aarch64, Raspberry Pi OS (Debian trixie), hostname `max`, user `maxwelllokshin`,
  internet only via a phone hotspot (can drop — see Risks; offline behavior is explicitly NOT a goal anymore).
- Ollama/qwen2.5:0.5b was measured and abandoned as the primary brain (see History). It is not part of the
  current design. If it's ever revisited, the old measurements (8-40 s/call, ~600 MB RAM, can't write prose or
  reason about the answer) are why it was dropped — don't re-attempt without addressing those.
- RAM budget is now dominated by whatever the Python process + its deps cost (no DDS, no colcon/ament, no
  Ollama) — expected to be comfortably small on a Pi 3, but re-measure for real once deployed (Phase 2).

## Architecture (current)
```
guest's phone browser (mic + Web Speech API STT)
        |  HTTPS (Tailscale Funnel or Cloudflare Tunnel in front of the Pi -- browsers block mic on plain HTTP)
        v
Pi hosts the webpage AND the API (pi_app/server.py)
        |  POST /speak {text}
        v
DecisionService (pi_app/decision_service.py):
  1. RequestBudget check (daily/total cap on decision calls)
  2. DecisionClient -> one OpenAI-compatible /chat/completions call,
     structured output {"snack": enum, "reply": string}
  3. CharacterBudget check (daily/total cap on TTS characters)
  4. TtsClient -> a SEPARATE OpenAI-compatible /audio/speech call -> mp3 bytes
  -> SnackMessage(request_id, snack_id, reply_text, audio_mp3, source)
        |
        v
EspLink.send() -> ESP32 over serial (newline-delimited JSON header + raw mp3 bytes)
        |
        v
ESP32 (not built here): owns snack_id -> slot/angle mapping, drives servos, (later) plays the audio
```

### Why two cloud calls, not one combined audio+decision call
OpenAI's audio-output models (the `modalities: ["text","audio"]` chat completions path) have historically had
uncertain/limited support for `response_format: json_schema` at the same time as audio output, and even if that
worked, the model's *spoken* audio would be a rendition of its own response text — there's no clean way to make
it speak only the mystical reply while *also* embedding a machine-parseable snack id in that same text without
either garbling the spoken line or needing fragile parsing. Two clean calls — a cheap structured-output decision
call (`gpt-5-nano`), then a dedicated TTS call (`gpt-4o-mini-tts` via `/audio/speech`, which returns raw mp3
bytes directly and takes an `instructions` field for steering tone) — is simpler, verified against current docs,
and reuses the already-tested structured-output code from step 2 of the history above.

### Why not a cloned custom voice (yet)
`gpt-4o-mini-tts` only offers a fixed voice list (alloy, ash, ballad, coral, echo, fable, onyx, nova, sage,
shimmer, verse, ...), steered via an `instructions` field (tone/persona), not true voice cloning. `TtsClient`
only depends on a `synthesize(text) -> bytes` shape, so swapping in an ElevenLabs-style voice-cloning provider
later (for a real custom "frog wizard" voice) is a drop-in replacement, not a redesign.

## Config: `pi_app/config/snacks.json` (single source of truth, editable when real snacks change)
Schema (validate on load with clear errors) — unchanged from earlier in the project:
```json
{
  "version": 1,
  "default_snack": "none",
  "snacks": [
    {"id":"cookies","label":"Cookies","slot":1,"angle":65,
     "aliases":["biscuit"],"tags":["sweet","soft","blue","rain"],
     "lines":[{"id":"cookies_1","text":"Ah, the ancient tides foretell... cookies!"}]},
    {"id":"none","label":"No snack","slot":0,"angle":0,"aliases":[],"tags":[],
     "lines":[{"id":"none_1","text":"The spirits are silent... try again, traveler."}]}
  ]
}
```
- Ship placeholder snacks: cookies, candy, chips, none.
- `label`/`tags`/`aliases` are still used (to build the decision call's system prompt, and to validate that its
  `reply` actually mentions the chosen snack) even though `lines` is no longer used in the normal path — the
  cloud LLM writes the reply itself now. `lines` stays in the schema/placeholder data as a reference/fallback
  asset, not because it's required.
- Hot reload: `SnacksStore` polls file mtime every 2 s (`server.py`'s background task). If the new file is
  invalid, KEEP the old config and log the error. Log a diff of added/removed snacks.
- `slot`/`angle` here are the Pi's view of the catalog (for building prompts); the ESP32's firmware will need
  its OWN physical slot/angle mapping (see esp32_firmware note below) — keep those in sync by snack id, but they
  don't have to be the literal same file.

## pi_app/ layout
- `snacks.py` — loader/validator/hot-reload (`SnacksConfig`, `Snack`, `Line`, `SnacksStore`). No dependencies
  beyond the stdlib.
- `request_budget.py` — `RequestBudget` (decision calls) and `CharacterBudget` (TTS characters). Both have a
  daily cap and a lifetime cap; `-1` means unlimited.
- `decision_client.py` — `DecisionClient.choose_snack_and_reply(text, snacks) -> (snack_id, reply)`. Structured
  output, temperature ~0.7, 6 s timeout, one retry only on 429/5xx. Validates the reply (non-empty, <=160 chars,
  no URL, mentions the chosen snack) — any failure raises `DecisionError`, which `DecisionService` treats as
  "use the default snack and a hardcoded apology line," never as something that reaches the guest as an error.
  System prompt explicitly frames the guest's text as DATA, never instructions (prompt-injection defense).
- `tts_client.py` — `TtsClient.synthesize(text) -> bytes`. `DecisionError`/`TtsError` subtypes map to
  timeout/request/invalid-response failures the same way as the decision client.
- `message_package.py` — `SnackMessage(request_id, snack_id, reply_text, audio_mp3, source)`. `audio_mp3` is
  `None` when TTS was skipped (budget) or failed — that's a normal, expected state, not an error state.
- `decision_service.py` — orchestrates the above into one `handle(request_id, text, snacks) -> SnackMessage`
  call. Never raises.
- `esp32_link.py` — `EspLink` interface, `NullEspLink` (dev/no hardware), `SerialEspLink` (pyserial). Protocol is
  a placeholder (newline-delimited JSON header + raw audio bytes) — the REAL wire protocol, chunking/flow
  control for audio, and ESP32-side ack/heartbeat handling still need a `docs/esp32_protocol.md` spec, same as
  originally planned, just not written yet.
- `server.py` — `aiohttp` app. `POST /speak {text}` (bearer-token auth via `WEB_BRIDGE_TOKEN`, 300-char cap) ->
  `DecisionService` (off the event loop via `run_in_executor`, since the HTTP clients are synchronous) ->
  `EspLink.send()` -> JSON response. `GET /healthz`. A background task polls `SnacksStore.maybe_reload()`.
  Does NOT yet serve the guest-facing frontend, rate limiting, CORS, or a WebSocket-style live state push — all
  still open (see What's missing).

## Secrets
`LLM_API_KEY` (decision calls), `TTS_API_KEY` (TTS calls, can be the same key as LLM_API_KEY if both are
OpenAI), `WEB_BRIDGE_TOKEN` (bearer auth on `/speak`) — from env or an untracked `secrets.env`
(`pi_app/secrets.env.example` is the template), never a hardcoded value, never logged above DEBUG, never sent to
or held by the browser.

## What's missing / still open
- `docs/esp32_protocol.md` + the real ESP32 firmware (separate session, per the original scope split).
- The guest-facing frontend (mic capture page) — Pi will host it, but the actual HTML/JS isn't built.
- Rate limiting, CORS config, and a WebSocket-style live `/events` push (original `web_bridge` design had these;
  current `server.py` is deliberately minimal).
- The HTTPS tunnel setup (Tailscale Funnel or Cloudflare Tunnel) in front of the Pi — needed because browsers
  block mic access on plain HTTP, and this doesn't go away just because the Pi hosts the page itself.
- systemd unit for autostart on boot, `EnvironmentFile=` for secrets.
- A real end-to-end test with live API keys (everything so far is tested against fake local HTTP servers).
- RAM re-measurement on the real Pi (Phase 2) — expected to be easy now, but unverified.

## Tests
- Unit: `snacks.json` validation + hot reload, `RequestBudget`/`CharacterBudget` caps, `DecisionClient` against a
  fake HTTP server (success/invalid snack/bad JSON/429-retry/non-retryable error/timeout/connection
  refused/content validation failures), `TtsClient` against a fake server (success/non-200/timeout/connection
  refused/payload shape), `DecisionService`'s budget/error/success paths, `EspLink` against a fake serial
  object, `server.py` via `aiohttp`'s test client (auth, validation, happy path).
- Still needed: a real integration test once the ESP32 firmware and frontend exist; `tools/smoke_pi.sh` (checks
  internet reachability and the env key *names*, never values, plus the serial device).

## Process
- Present a short plan first, then build incrementally, committing after each piece with a working test.
- Ask questions only if blocked. Otherwise choose sensible defaults, state them, and keep going.
- Finish with a README covering: setup on the Pi, how to swap snacks (edit snacks.json, hot-reloads
  automatically), how to run tests, and a prioritized list of risks and recommendations.

## Risks and recommendations (living list)
- **Total network dependence.** With offline/keyword/local explicitly dropped, there is now NO path that works
  without internet — not just the LLM/TTS calls, but the webpage itself if it's only reachable through a tunnel.
  A dropped hotspot means the whole thing is down, not degraded. This was a deliberate choice, not an oversight
  — just making sure it's written down as one.
- **Two cloud cost surfaces.** Decision calls and TTS calls are now separate API costs, both per-request, and
  this is a publicly reachable endpoint. `RequestBudget`/`CharacterBudget` caps exist in code but need real
  values set before going live, not left at defaults.
- **Key security.** `LLM_API_KEY`/`TTS_API_KEY`/`WEB_BRIDGE_TOKEN` from env/`secrets.env` only, never logged
  above DEBUG, never sent to the browser (it only captures mic audio and calls `/speak` with its own token).
- **Prompt injection.** The guest's text reaches the decision LLM as untrusted input; the system prompt frames
  it as data, and the structured-output schema + reply validation (must be a real snack, must mention it, no
  URL, length-capped) bound what a successful injection could even produce.
- **No audio pregeneration anymore.** Every reply is freshly LLM-written, so there's no cache hit path — every
  successful request pays a live TTS call's latency and cost, with no way around it now that pre-written lines
  aren't in the normal path.
