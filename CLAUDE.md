# Project: Wizard snack animatronic — Pi-side service

## What we're building
A voice-quiz animatronic, hackathon team project ("Tubi the Wizard Frog"). A teammate's website (phone-first,
mic capture + browser speech-to-text, in `website/`) sends the guest's transcribed text over a WebSocket. This
Pi-side service (`pi_app/`) is "the teammate system" on the other end of that socket: it picks a snack, writes
and voices the spoken reply via two cloud calls, plays the audio out of a speaker wired to the Pi, and pulses an
ESP32 (firmware in `esp32_firmware/`, built by another teammate) over serial to open/close a trap door. This
task covers ONLY `pi_app/`. Do not write inside `esp32_firmware/` or `website/` — those are built by teammates;
treat their contents as a contract to integrate with, not code to edit.

## History (why the design looks like this)
Four real pivots happened while building this, in order — kept here so a reader isn't confused by
code/decisions that look like they contradict each other:
1. **Started as a ROS 2 workspace** with a local Ollama LLM (`qwen2.5:0.5b`) as the "brain". Measured on the real
   Pi: ~2-3 tok/s generation, 8-40 s per call, and the model could not reliably reason "crunchy" -> a snack or
   write coherent prose. Memory was dominated by Ollama (~600 MB of 905 MiB total).
2. **Replaced Ollama with a cloud LLM** (OpenAI-compatible, structured output) as the primary decision-maker,
   keeping Ollama as an optional off-by-default `local` fallback provider alongside `keyword` and `fallback`,
   inside a ROS 2 node.
3. **Dropped ROS 2 and the offline/keyword/local path entirely.** The whole pipeline became a linear
   request -> cloud call(s) -> serial write — ROS 2's DDS discovery and per-node overhead wasn't earning its
   memory cost for that, and offline resilience was explicitly deprioritized in favor of the cloud LLM deciding
   everything, including writing the spoken reply itself (no more pre-written lines in the normal path).
4. **Teammates' real `esp32_firmware/` and `website/` code landed, and both contradicted assumptions made
   without seeing them:**
   - The ESP32 (`esp32_firmware/src/motor_esp2`, ESP-IDF) reads ONE raw ASCII byte over UART0 at 115200 baud —
     `b'1'` closes a trap door, `b'2'` opens it. No JSON, no ack, no audio support at all. There's also no
     per-snack slot routing yet (constants for a rotating tray exist but aren't wired to any input) — so
     `angle` was dropped from `snacks.json` entirely (the firmware doesn't take one) in favor of a plain `slot`
     int, carried through for whenever that routing gets built.
   - Audio therefore plays out of a speaker wired directly to the Pi, not through the ESP32.
   - The website (`website/`, Node/Express/`ws`) is a WebSocket **broadcast hub**: the browser and this
     teammate system are both just clients of the same socket (see `website/docs/message-contract.md`). This
     Pi service connects OUT to it, rather than running its own inbound HTTP server — `server.py` (an inbound
     `POST /speak` server) was wrong and was replaced by `teammate_client.py` (an outbound WS client).
   - TTS moved from OpenAI's `/audio/speech` to ElevenLabs, per a direct ask, as a stepping stone toward a
     cloned custom voice later (ElevenLabs supports voice cloning; OpenAI's TTS voices don't).

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
guest's phone browser (website/, a teammate's — mic + Web Speech API STT)
        |  WebSocket: {"type":"transcript","payload":{"text":...}}, broadcast to all clients
        v
pi_app/teammate_client.py -- connects OUT to the website's WS hub as "the teammate system"
        |
        v
DecisionService (pi_app/decision_service.py):
  1. RequestBudget check (daily/total cap on decision calls)
  2. DecisionClient -> one OpenAI-compatible /chat/completions call,
     structured output {"snack": enum, "reply": string}
  3. CharacterBudget check (daily/total cap on TTS characters)
  4. TtsClient -> a SEPARATE ElevenLabs /text-to-speech/{voice_id} call -> mp3 bytes
  -> SnackMessage(request_id, snack_id, slot, reply_text, audio_mp3, source)
        |
        +--> ws.send_json({"type":"display",...}) / {"type":"state","payload":{"speaking":...}} back to the website
        +--> AudioPlayer.play(audio_mp3) -- out of a speaker wired to the Pi itself (NOT the ESP32)
        +--> EspLink.open_trap_door() / close_trap_door() -- single ASCII byte over serial to the ESP32
```
The guest-facing frontend lives entirely in `website/` (a teammate's). The HTTPS-for-mic-access requirement and
the website's own hosting choice are that teammate's call per `website/CLAUDE.md` — read it before assuming
anything about where/how the site runs.

### Why two cloud calls, not one combined audio+decision call
Audio-output chat models' structured-output support is unverified/historically limited, and even where
supported, the model's *spoken* audio is a rendition of its own response text — there's no clean way to make it
speak only the mystical reply while also embedding a machine-parseable snack id in that same text without either
garbling the spoken line or needing fragile parsing. Two clean calls — a cheap structured-output decision call
(`gpt-5-nano`), then a dedicated TTS call — is simpler and keeps each call's job single-purpose.

### Why ElevenLabs for TTS, and not a cloned voice yet
Switched from OpenAI's `/audio/speech` to ElevenLabs specifically because ElevenLabs supports voice cloning and
OpenAI's TTS voices don't — this is a deliberate stepping stone. Today it uses a stock premade voice (`"Adam"`,
id `pNInz6obpgDQGcFmaJgB`) steered via `stability`/`similarity_boost`. `TtsClient` only depends on a
`synthesize(text) -> bytes` shape, so swapping `voice_id` for a real cloned "frog wizard" voice later is a
one-line config change, not a redesign.

## Config: `pi_app/config/snacks.json` (single source of truth, editable when real snacks change)
Schema (validate on load with clear errors):
```json
{
  "version": 1,
  "default_snack": "none",
  "snacks": [
    {"id":"cookies","label":"Cookies","slot":1,
     "aliases":["biscuit"],"tags":["sweet","soft","blue","rain"],
     "lines":[{"id":"cookies_1","text":"Ah, the ancient tides foretell... cookies!"}]},
    {"id":"none","label":"No snack","slot":0,"aliases":[],"tags":[],
     "lines":[{"id":"none_1","text":"The spirits are silent... try again, traveler."}]}
  ]
}
```
- No `angle` field (dropped — see History #4; the ESP32 doesn't take one). `slot` is a plain int, carried
  through `SnackMessage` for whenever the firmware's per-slot routing gets built.
- `label`/`tags`/`aliases` are still used (to build the decision call's system prompt, and to validate that its
  `reply` actually mentions the chosen snack) even though `lines` is no longer used in the normal path — the
  cloud LLM writes the reply itself now. `lines` stays in the schema/placeholder data as a reference/fallback
  asset, not because it's required.
- Hot reload: `SnacksStore` polls file mtime every 2 s. If the new file is invalid, KEEP the old config and log
  the error. Log a diff of added/removed snacks.
- Current placeholder data has a real bug worth fixing: two snacks share `slot: 3` (a physical conflict) — not
  changed here since it's content, not code, but flag it before this goes further.

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
- `tts_client.py` — ElevenLabs. `TtsClient.synthesize(text) -> bytes`. `TtsError` subtypes map
  timeout/request/invalid-response failures the same way as the decision client.
- `audio_player.py` — `AudioPlayer.play(mp3_bytes)`. Shells out to `afplay`/`mpg123`/`ffplay` (whichever is
  found) rather than pulling in a Python audio dependency. Never raises; missing player or playback failure just
  means no audio played, same severity as a TTS failure.
- `message_package.py` — `SnackMessage(request_id, snack_id, slot, reply_text, audio_mp3, source)`. `audio_mp3`
  is `None` when TTS was skipped (budget) or failed — that's a normal, expected state, not an error state.
- `decision_service.py` — orchestrates the above into one `handle(request_id, text, snacks) -> SnackMessage`
  call. Never raises.
- `esp32_link.py` — `EspLink` interface (`open_trap_door()`/`close_trap_door()`), `NullEspLink` (dev/no
  hardware), `SerialEspLink` (pyserial, writes a single `b'1'`/`b'2'` byte at 115200 baud — matches the real
  firmware exactly, see History #4). No per-slot routing yet; `SnackMessage.slot` is unused here until the
  firmware grows that.
- `teammate_client.py` — `TeammateClient`, an `aiohttp` WS **client** connecting OUT to `WEBSITE_WS_URL`.
  Ignores every message except `{"type":"transcript",...}`; on one, runs `DecisionService` off the event loop
  (`run_in_executor`), sends `display`/`state` messages back, plays audio locally, then pulses the trap door
  open then closed (`DISPENSE_OPEN_SECONDS`). Auto-reconnects with a fixed delay on disconnect.
- `tools/try_request.py` — standalone CLI, no website/ESP32 needed: runs one piece of text through the real
  `DecisionService` (real API calls) and prints the resulting package + saves the audio to a file. This is the
  "test without either" path.

## Secrets
`LLM_API_KEY` (decision calls, OpenAI-compatible), `TTS_API_KEY` (ElevenLabs) — from env or an untracked
`secrets.env` (`pi_app/secrets.env.example` is the template), never a hardcoded value, never logged above DEBUG.
`WEBSITE_WS_URL` and `ESP32_SERIAL_PORT` are plain config, not secrets, but also env-driven (see
`secrets.env.example`) so nothing is hardcoded.

## What's missing / still open
- The real per-slot ESP32 routing (firmware only supports one trap door open/close right now, not a snack
  selector) — `SnackMessage.slot` is ready for it, `esp32_link.py` isn't yet.
- The duplicate `slot: 3` bug in `pi_app/config/snacks.json` (two snacks claim the same physical position).
- `docs/esp32_protocol.md` — never written; the real protocol turned out to be much simpler (one byte) than
  originally planned (JSON + chunked audio), so this may not even be needed unless the protocol grows.
- A real end-to-end test with live API keys and the actual website + ESP32 (everything so far is tested against
  fake local HTTP/WS servers and a fake serial object) — use `tools/try_request.py` to test the cloud-call half
  without either.
- systemd unit for autostart on boot, `EnvironmentFile=` for secrets.
- RAM re-measurement on the real Pi (Phase 2) — expected to be easy now, but unverified.
- Coordinating with the website teammate on exactly where/how the site is hosted (their `CLAUDE.md` currently
  assumes Render, which is a different plan than "the Pi hosts it") and the HTTPS tunnel in front of it.

## Tests
- Unit: `snacks.json` validation + hot reload, `RequestBudget`/`CharacterBudget` caps, `DecisionClient` against a
  fake HTTP server (success/invalid snack/bad JSON/429-retry/non-retryable error/timeout/connection
  refused/content validation failures), `TtsClient` against a fake ElevenLabs-shaped server (success/non-200/
  timeout/connection refused/payload shape), `DecisionService`'s budget/error/success paths, `EspLink` against a
  fake serial object, `AudioPlayer` against fake player commands, `TeammateClient` both via direct
  `handle_message()` calls and one real-websocket round-trip test against an in-process `aiohttp` test server.
- Still needed: a real integration test once the website and ESP32 are both reachable together;
  `tools/smoke_pi.sh` (checks internet reachability and the env key *names*, never values, plus the serial
  device).

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
- **Key security.** `LLM_API_KEY`/`TTS_API_KEY` from env/`secrets.env` only, never logged above DEBUG, never sent
  to the browser — the browser only ever talks to the website's own server, never directly to this service.
- **Prompt injection.** The guest's text reaches the decision LLM as untrusted input; the system prompt frames
  it as data, and the structured-output schema + reply validation (must be a real snack, must mention it, no
  URL, length-capped) bound what a successful injection could even produce.
- **No audio pregeneration anymore.** Every reply is freshly LLM-written, so there's no cache hit path — every
  successful request pays a live TTS call's latency and cost, with no way around it now that pre-written lines
  aren't in the normal path.
