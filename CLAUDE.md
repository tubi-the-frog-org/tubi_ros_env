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
5. **Replaced the one-shot "text in, snack+reply out" shape with a real multi-turn conversation.** Tubi now
   introduces himself (with a running joke: he always gets one detail of his own backstory comically wrong),
   asks the guest to introduce themselves, asks a few open-ended personality/feeling questions of his own
   choosing, waits for an explicit confirmation, then reveals a snack with a personality-test-style percentage
   breakdown across every snack — not just the winner. One endpoint, one schema, grown to support this: the
   `{"snack","reply"}` schema gained `done` (keep talking vs. reveal now) and `percentages`, since a single
   call can no longer always commit to a snack. `conversation_session.py` owns the growing chat history;
   `DecisionClient.converse()` is the call. `website/docs/message-contract.md` only documents the old one-shot
   shape, so `teammate_client.py` now sends a `speak` message per turn and an extended `display` — see
   `pi_app/docs/multi_turn_contract_proposal.md` for exactly what the website side needs to add to really drive
   this (not yet built there). `tools/talk_to_tubi.py` exercises the whole conversation locally, by voice, using
   the dev machine's own microphone + local Whisper STT — no website, ESP32, or Pi needed to test it.

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
        |  session:start -> new ConversationSession; transcript -> .respond(); session:end -> clear
        v
ConversationSession (pi_app/conversation_session.py), one per guest, owns the growing chat history:
  per turn -> RequestBudget check -> DecisionClient.converse(history, snacks)
              -> {"done": bool, "snack": enum|"undecided", "reply": string, "percentages": {...}}
  every turn -> CharacterBudget check -> TtsClient.synthesize(reply) -> mp3 bytes (ElevenLabs)
        |
        +--> every turn: ws.send_json({"type":"speak","payload":{"text":reply}}),
        |    {"type":"state","payload":{"speaking":...}}, AudioPlayer.play(audio) on the Pi's own speaker
        +--> only on done=true: {"type":"display","payload":{"kind":"result","snack":...,"percentages":{...}}},
             EspLink.open_trap_door()/close_trap_door() (single ASCII byte over serial), write the
             QuizResult JSON to tools/quiz_result.json
```
The guest-facing frontend lives entirely in `website/` (a teammate's). The HTTPS-for-mic-access requirement and
the website's own hosting choice are that teammate's call per `website/CLAUDE.md` — read it before assuming
anything about where/how the site runs. The `speak` message and `display`'s `percentages` field are a proposal
(`pi_app/docs/multi_turn_contract_proposal.md`), not yet implemented on the website side.

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
- `decision_client.py` — two call shapes against the same OpenAI-compatible endpoint:
  - `choose_snack_and_reply(text, snacks) -> (snack_id, reply)` — the original one-shot shape. Still used by
    `decision_service.py`/`tools/try_request.py` for a quick single-question test in isolation.
  - `converse(messages, snacks) -> ConversationTurn` — the real multi-turn shape, used by
    `ConversationSession`. Schema adds `done` and `percentages` (see History #5). Percentages are
    re-normalized in code (`_normalize_percentages`) to always sum to 100, since the model's raw numbers often
    don't.
  Both: 15 s timeout, one retry only on 429/5xx (`temperature` omitted by default — `gpt-5-nano` 400s on
  anything but its default of 1; `reasoning_effort` defaults to `"low"`). Reply validation (non-empty, no URL;
  <=160 chars + must-mention-the-snack only for a one-shot reply or a `done=true` reveal line; a more lenient
  length-only cap for intermediate conversational turns) — any failure raises `DecisionError`, treated as "say
  a hardcoded apology line," never something that reaches the guest as a crash. System prompt frames the
  guest's text as DATA, never instructions (prompt-injection defense). **Measured with real keys**:
  `reasoning_effort` matters a lot for GPT-5-family models — default effort took ~11-20 s/call; `"minimal"`
  dropped that to ~2 s/call but was unreliable (3/5 trials never actually committed to a snack on reveal,
  staying `"undecided"`); `"low"` lands at ~3-8 s/call with 10/11 trials completing successfully. `"low"` is
  the default; `"minimal"` is tempting for speed but measurably trades away correctness on this task.
- `conversation_session.py` — `ConversationSession`, one per guest: owns the growing chat-history list,
  `.start()` kicks off Tubi's intro, `.respond(guest_text)` feeds one guest turn and returns Tubi's next line.
  When a turn comes back `done=true`, `.result` is set to a `QuizResult` (session_id, chosen_snack_id, slot,
  percentages, transcript) and further `.respond()` calls raise. On a `DecisionError`, rolls back the
  just-appended guest turn before returning a fallback line, so a failed call doesn't poison the history the
  next real call sees.
- `tts_client.py` — ElevenLabs. `TtsClient.synthesize(text) -> bytes`. `TtsError` subtypes map
  timeout/request/invalid-response failures the same way as the decision client.
- `audio_player.py` — `AudioPlayer.play(mp3_bytes)`. Shells out to `afplay`/`mpg123`/`ffplay` (whichever is
  found) rather than pulling in a Python audio dependency. Never raises; missing player or playback failure just
  means no audio played, same severity as a TTS failure.
- `message_package.py` / `decision_service.py` — the older one-shot pipeline (`SnackMessage`,
  `DecisionService.handle()`). Kept for `tools/try_request.py`'s quick single-question tests; not what drives
  a real conversation anymore (see `teammate_client.py`/`conversation_session.py`).
- `esp32_link.py` — `EspLink` interface (`open_trap_door()`/`close_trap_door()`), `NullEspLink` (dev/no
  hardware), `SerialEspLink` (pyserial, writes a single `b'1'`/`b'2'` byte at 115200 baud — matches the real
  firmware exactly, see History #4). No per-slot routing yet; `QuizResult.slot` is unused here until the
  firmware grows that.
- `teammate_client.py` — `TeammateClient`, an `aiohttp` WS **client** connecting OUT to `WEBSITE_WS_URL`.
  `session:start` creates a `ConversationSession` and speaks its intro; `transcript` feeds the active session
  and speaks the reply; on `done`, sends the extended `display` (with `percentages`), pulses the trap door
  (`DISPENSE_OPEN_SECONDS`), and writes the `QuizResult` to `tools/quiz_result.json`. Both the decision call and
  the TTS call are budget-gated per turn (`RequestBudget`/`CharacterBudget`), degrading to a fallback line or
  text-only rather than erroring. Auto-reconnects with a fixed delay on disconnect.
- `tools/try_request.py` — standalone CLI, no website/ESP32 needed: runs one piece of text through the real
  one-shot `DecisionService` (real API calls) and prints the resulting package + saves the audio to a file.
  Good for a quick isolated check of the decision+TTS pipeline, not the real conversation.
- `tools/talk_to_tubi.py` — the real "test without either" path: have the actual multi-turn conversation by
  voice, using the dev machine's own mic + local Whisper STT (matching `website/python/stt_worker.py`'s
  approach, so it doesn't spend API credits on transcription) — no website, ESP32, or Pi needed. Needs
  `tools/requirements-mic.txt` installed separately (SpeechRecognition/pyaudio/openai-whisper — deliberately
  NOT in `requirements.txt`, since whisper pulls in torch and has no business running on a Pi 3).

## Secrets
`LLM_API_KEY` (decision calls, OpenAI-compatible), `TTS_API_KEY` (ElevenLabs) — from env or an untracked
`secrets.env` (`pi_app/secrets.env.example` is the template), never a hardcoded value, never logged above DEBUG.
`WEBSITE_WS_URL` and `ESP32_SERIAL_PORT` are plain config, not secrets, but also env-driven (see
`secrets.env.example`) so nothing is hardcoded.

## What's missing / still open
- The `speak` message and `display.percentages` addition (`pi_app/docs/multi_turn_contract_proposal.md`) aren't
  built on the website side yet — `teammate_client.py` sends them, but nothing there re-arms the mic after each
  Tubi turn or displays the running conversation. Real end-to-end multi-turn needs that built first.
- The real per-slot ESP32 routing (firmware only supports one trap door open/close right now, not a snack
  selector) — `QuizResult.slot` is ready for it, `esp32_link.py` isn't yet.
- The duplicate `slot: 3` bug in `pi_app/config/snacks.json` (two snacks claim the same physical position).
- `docs/esp32_protocol.md` — never written; the real protocol turned out to be much simpler (one byte) than
  originally planned (JSON + chunked audio), so this may not even be needed unless the protocol grows.
- A real end-to-end test with live API keys and the actual website + ESP32 — use `tools/talk_to_tubi.py` to test
  the real conversation by voice without either, or `tools/try_request.py` for a single-question check.
- systemd unit for autostart on boot, `EnvironmentFile=` for secrets.
- RAM re-measurement on the real Pi (Phase 2) — expected to be easy now, but unverified.
- Coordinating with the website teammate on exactly where/how the site is hosted (their `CLAUDE.md` currently
  assumes Render, which is a different plan than "the Pi hosts it") and the HTTPS tunnel in front of it.

## Tests
- Unit: `snacks.json` validation + hot reload, `RequestBudget`/`CharacterBudget` caps, `dotenv.py`'s loader,
  `DecisionClient` against a fake HTTP server — both `choose_snack_and_reply` (success/invalid snack/bad
  JSON/429-retry/non-retryable error/timeout/connection refused/content validation) and `converse`
  (not-done/done/percentage-normalization/invalid-snack/reply-doesn't-mention-snack/too-long/bad-json),
  `TtsClient` against a fake ElevenLabs-shaped server, `DecisionService`'s budget/error/success paths,
  `ConversationSession` against a stub client (history accumulation, reaching done, rollback-on-error,
  raising if you `.respond()` after done), `EspLink` against a fake serial object, `AudioPlayer` against fake
  player commands, `TeammateClient` via direct `handle_message()` calls (session:start/transcript/session:end,
  budget exhaustion on both budgets, reveal+dispense+file-write) and one real-websocket round-trip test.
- Still needed: a real integration test once the website contract addition and ESP32 are both built;
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
