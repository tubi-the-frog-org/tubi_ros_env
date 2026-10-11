# Project: Tubi the Wizard Frog — magic-user assessment

## What we're building
A voice-conversation animatronic, hackathon team project ("Tubi the Wizard Frog"). A teammate's website
(`website/`) sends guest transcripts to the Pi service (`pi_app/`) over a WebSocket. Tubi asks personality
questions, then the AI determines a magic-user type and level and writes Tubi's spoken reply. ElevenLabs
generates the voice; the Pi plays it and uses the ESP32 for speaking animation. The hardware also retains
servo and dispenser code, but the AI decision flow does not invoke dispensing. Work here is scoped to
the Pi-side AI service; the website and ESP32 implementations are teammate-owned unless explicitly authorized.

## History (why the design looks like this)
Four real pivots happened while building this, in order — kept here so a reader isn't confused by
code/decisions that look like they contradict each other:
1. **Started as a ROS 2 workspace** with a local Ollama LLM (`qwen2.5:0.5b`) as the "brain". Measured on the real
  Pi: ~2-3 tok/s generation and 8-40 s per call, and the model could not reliably interpret short guest
  answers or write coherent prose. Memory was dominated by Ollama (~600 MB of 905 MiB total).
2. **Replaced Ollama with a cloud LLM** (OpenAI-compatible, structured output) as the primary decision-maker,
   keeping Ollama as an optional off-by-default `local` fallback provider alongside `keyword` and `fallback`,
   inside a ROS 2 node.
3. **Dropped ROS 2 and the offline/keyword/local path entirely.** The whole pipeline became a linear
   request -> cloud call(s) -> serial write — ROS 2's DDS discovery and per-node overhead wasn't earning its
   memory cost for that, and offline resilience was explicitly deprioritized in favor of the cloud LLM deciding
   everything, including writing the spoken reply itself (no more pre-written lines in the normal path).
4. **Teammates' real `esp32_firmware/` and `website/` code landed, and both contradicted assumptions made
   without seeing them:**
   - The ESP32 (`esp32_firmware/src/motor_esp2`, ESP-IDF) provides servo and dispenser controls over UART.
     These remain hardware functionality; the current AI assessment does not issue dispense commands.
   - Audio therefore plays out of a speaker wired directly to the Pi, not through the ESP32.
   - The website (`website/`, Node/Express/`ws`) is a WebSocket **broadcast hub**: the browser and this
     teammate system are both just clients of the same socket (see `website/docs/message-contract.md`). This
     Pi service connects OUT to it, rather than running its own inbound HTTP server — `server.py` (an inbound
     `POST /speak` server) was wrong and was replaced by `teammate_client.py` (an outbound WS client).
   - TTS moved from OpenAI's `/audio/speech` to ElevenLabs, per a direct ask, as a stepping stone toward a
     cloned custom voice later (ElevenLabs supports voice cloning; OpenAI's TTS voices don't).
5. **Moved from one-shot input to a multi-turn conversation.** Tubi introduces himself, asks the guest's name,
  and asks a few open-ended personality questions. When ready, he gives a fictional magic-user type and a
  freely chosen level from 1 to 10; this is not calculated or presented as a real aptitude score. The spoken
  reveal connects to something the guest actually said. `conversation_session.py` owns chat history and
  `DecisionClient.converse()` returns the structured fields `reasoning`, `done`, `magic_type`, `magic_level`,
  and `reply`. `teammate_client.py` speaks each turn and emits a final `magic-assessment` display message.
  `tools/talk_to_tubi.py` exercises the multi-turn conversation locally using the dev machine's microphone
  and local Whisper transcription.
6. **Several rounds of real-voice testing reshaped the system prompt** (`_SYSTEM_PROMPT_TEMPLATE` in
   `decision_client.py`) and added a real cloned voice. In order, each measured against live calls/real mic
   audio, not just reasoned about:
   - Personality rewrite: Tubi is goofy-but-smart/eccentric, gives a brief in-character intro, asks one
     whimsical open-ended question per turn, and reacts briefly to each answer.
   - "Ask a few questions" / "reveal when confident" were, again, too soft — tightened to an explicit "at most
    4 questions, treat a 5th as a mistake." The prompt controls question count and reveal timing; there is no
    code-injected question bank or forced reveal.
   - **Guest answers come from speech-to-text and will often be garbled, mistranscribed, or off-topic** —
     measured a real failure mode where a mistranscribed stray character got taken literally and the model
     hallucinated an entire new unrelated question out of it, derailing the rest of the conversation. The
     prompt now explicitly says answers may be garbled, not to take them literally, and to rephrase or move on
     rather than spiraling.
   - **Voice cloning**: `tools/clone_voice.py` (new) is a one-time setup script — NOT something the running
     service ever calls — that uploads a local audio sample (`config/max.mp3`) via the `elevenlabs` SDK
     (`tools/requirements-voice-clone.txt`, kept out of core `requirements.txt` same as the mic tooling) and
     prints a `voice_id` to put in `secrets.env` as `TTS_VOICE_ID`. Two real bugs fixed getting this working:
     the ElevenLabs API key needed its `Voices` permission raised from "No Access" to "Write" on their
     dashboard (an account-side fix, not code), and the installed `elevenlabs` SDK (2.71.0) has a bug where
     omitting the `labels` argument (or passing `None`) fails server-side with "Labels must be serialized
     dictionary object" instead of being treated as absent — an explicit `labels={}` works around it. A real
     cloned voice now exists and is wired in and verified producing audio.

7. **Firmware grew to 4 servos + a line protocol** (user-requested edit to `esp32_firmware/.../pwm.c`, the one
   exception to the "don't touch esp32_firmware" rule). Head/arm/lazy susan/trap door each have an MCPWM channel;
   commands are text lines (`bin 3`, `door open`, `talk <ms per word...>`, plus a `test` mode with raw angles) —
   the old single-byte `b'1'`/`b'2'` protocol in History #4 and `esp32_link.py` is gone. Per-word timings in
   `talk` are an estimate (audio bytes / 16000 B/s, split by word length). Pose angles in `pwm.c` are
   placeholders until tuned in test mode.
   Two independent state machines (talk: head/arm; dispense: lazy susan/door) run in the same 20 ms loop. The reveal
   is one command, `dispense <slot>`: rotate to the bin, open the door, hold, close — the ESP32 runs it itself.

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
  per turn -> RequestBudget check -> DecisionClient.converse(history)
              -> {"done": bool, "magic_type": string, "magic_level": 1..10, "reply": string}
              (type and level are empty/zero until Tubi decides to reveal; no confirmation step)
  every turn -> CharacterBudget check -> TtsClient.synthesize(reply) -> mp3 bytes (ElevenLabs)
        |
        +--> every turn: ws.send_json({"type":"speak","payload":{"text":reply}}),
        |    {"type":"state","payload":{"speaking":...}}, AudioPlayer.play(audio) on the Pi's own speaker
           +--> only on done=true: {"type":"display","payload":{"kind":"magic-assessment","magic_type":...,"magic_level":...}},
             write the MagicResult JSON to tools/magic_result.json; no dispenser command is sent
```
The guest-facing frontend lives entirely in `website/` (a teammate's). The HTTPS-for-mic-access requirement and
the website's own hosting choice are that teammate's call per `website/CLAUDE.md` — read it before assuming
anything about where/how the site runs. The `magic-assessment` display payload is documented in
`pi_app/docs/multi_turn_contract_proposal.md`; a dedicated website renderer may still be needed.

### Why two cloud calls, not one combined audio+decision call
Audio-output chat models' structured-output support is unverified/historically limited, and even where
supported, the model's *spoken* audio is a rendition of its own response text — there's no clean way to make it
speak only the mystical reply while also embedding machine-readable magic assessment fields in that same text without either
garbling the spoken line or needing fragile parsing. Two clean calls — a cheap structured-output decision call
(`gpt-5-nano`), then a dedicated TTS call — is simpler and keeps each call's job single-purpose.

### Why ElevenLabs for TTS
Switched from OpenAI's `/audio/speech` to ElevenLabs specifically because ElevenLabs supports voice cloning and
OpenAI's TTS voices don't. A real cloned voice now exists (see History #6) — `TtsClient` only ever depended on
a `synthesize(text) -> bytes` shape regardless, so going from the stock `"Adam"` voice (id
`pNInz6obpgDQGcFmaJgB`) to the cloned one was a one-line config change (`TTS_VOICE_ID` in `secrets.env`), not a
redesign. `tools/clone_voice.py` is how the clone itself gets created — see `pi_app/` layout below.

## Magic assessment
The model returns a freeform `magic_type` and integer `magic_level` from 1 to 10. Before the reveal,
`magic_type` is empty and `magic_level` is 0. The choice is playful, not scored or calculated. Tubi's
personality, questions, conversation flow, and answer-linked explanation stay in `_SYSTEM_PROMPT_TEMPLATE`.

## pi_app/ layout
- `request_budget.py` — `RequestBudget` (decision calls) and `CharacterBudget` (TTS characters). Both have a
  daily cap and a lifetime cap; `-1` means unlimited.
- `decision_client.py` — `assess_magic_user(text)` for a one-shot assessment and
  `converse(messages)` for the multi-turn flow. Structured output contains `magic_type`, `magic_level`,
  `reply`, and `done` (plus private `reasoning` during conversation). Magic level is validated as an integer
  from 1 to 10. Both calls use a 15 s timeout and one retry only on 429/5xx (`temperature` omitted by default — `gpt-5-nano` 400s on
  anything but its default of 1; `reasoning_effort` defaults to `"low"`). Reply validation (non-empty, no URL;
  a 280-character reveal limit and a 500-character intermediate limit truncated at sentence/word boundaries)
  — any failure raises `DecisionError`. The system prompt frames guest text as DATA, never instructions.
  **Measured with real keys**:
  `reasoning_effort` matters a lot for GPT-5-family models — default effort took ~11-20 s/call; `"minimal"`
  dropped that to ~2 s/call but was unreliable (3/5 trials returned invalid assessment output,
  staying `"undecided"`); `"low"` lands at ~3-8 s/call with 10/11 trials completing successfully. `"low"` is
  the default; `"minimal"` is tempting for speed but measurably trades away correctness on this task.
- `conversation_session.py` — `ConversationSession` owns one guest's growing history. On `done=true`, it stores
  a `MagicResult` with type, level, and transcript. Decision errors roll back the unanswered guest turn and
  propagate; the system prompt controls conversation questions and reveal timing.
- `tts_client.py` — ElevenLabs. `TtsClient.synthesize(text) -> bytes`. `TtsError` subtypes map
  timeout/request/invalid-response failures the same way as the decision client.
- `audio_player.py` — `AudioPlayer.play(mp3_bytes)`. Shells out to `afplay`/`mpg123`/`ffplay` (whichever is
  found) rather than pulling in a Python audio dependency. Never raises; missing player or playback failure just
  means no audio played, same severity as a TTS failure.
- `message_package.py` / `decision_service.py` — one-shot magic assessment and TTS pipeline used by
  `tools/try_request.py`.
- `esp32_link.py` — retained serial interface for speech animation and existing servo/dispenser commands.
  The magic assessment flow uses talking animation only and never issues a dispense command.
- `teammate_client.py` — `TeammateClient`, an `aiohttp` WS **client** connecting OUT to `WEBSITE_WS_URL`.
  `session:start` creates a `ConversationSession` and speaks its intro; `transcript` feeds the active session
  and speaks the reply; on `done`, sends the magic type/level display message and writes `MagicResult` to
  `tools/magic_result.json`. It does not call the hardware dispense API.
  Both the decision call and the TTS call are budget-gated per turn (`RequestBudget`/`CharacterBudget`),
  degrading to a fallback line or text-only rather than erroring. Auto-reconnects with a fixed delay on
  disconnect.
- `tools/try_request.py` — standalone CLI, no website/ESP32 needed: runs one piece of text through the real
  one-shot `DecisionService` (real API calls) and prints the resulting package + saves the audio to a file.
  Good for a quick isolated check of the decision+TTS pipeline, not the real conversation.
- `tools/talk_to_tubi.py` — the real "test without either" path: have the actual multi-turn conversation by
  voice, using the dev machine's own mic + local Whisper STT (matching `website/python/stt_worker.py`'s
  approach, so it doesn't spend API credits on transcription) — no website, ESP32, or Pi needed. Needs
  `tools/requirements-mic.txt` installed separately (SpeechRecognition/pyaudio/openai-whisper — deliberately
  NOT in `requirements.txt`, since whisper pulls in torch and has no business running on a Pi 3).
- `tools/clone_voice.py` — one-time setup, not a runtime path: uploads `config/max.mp3` via the `elevenlabs`
  SDK to create a cloned voice, prints the resulting `voice_id` to put in `secrets.env` as `TTS_VOICE_ID`. Needs
  `tools/requirements-voice-clone.txt` (same "separate from core requirements" pattern). Passes `labels={}`
  explicitly to `voices.ivc.create()` — required, not optional, due to an SDK bug (see History #6).

## Secrets
`LLM_API_KEY` (decision calls, OpenAI-compatible), `TTS_API_KEY` (ElevenLabs — needs the `Voices: Write`
permission on the dashboard if you ever need to re-run `tools/clone_voice.py`, not just text-to-speech) — from
env or an untracked `secrets.env` (`pi_app/secrets.env.example` is the template), never a hardcoded value,
never logged above DEBUG. `TTS_VOICE_ID` is set to the real cloned "Tubi" voice (see History #6) — not a
secret itself, but lives in `secrets.env` alongside the keys since it's config, not committed content.
`WEBSITE_WS_URL` and `ESP32_SERIAL_PORT` are plain config too, also env-driven, so nothing is hardcoded.

## What's missing / still open
- The website may need to render the `magic-assessment` display payload and re-arm the mic after each Tubi turn;
  see `pi_app/docs/multi_turn_contract_proposal.md`.
- ESP32 servo and dispenser controls remain available for separate hardware use; the AI flow does not invoke them.
- `docs/esp32_protocol.md` — never written; the real protocol turned out to be much simpler (one byte) than
  originally planned (JSON + chunked audio), so this may not even be needed unless the protocol grows.
- A real end-to-end test with live API keys and the actual website + ESP32 — use `tools/talk_to_tubi.py` to test
  the real conversation by voice without either, or `tools/try_request.py` for a single-question check.
- systemd unit for autostart on boot, `EnvironmentFile=` for secrets.
- RAM re-measurement on the real Pi (Phase 2) — expected to be easy now, but unverified.
- Coordinating with the website teammate on exactly where/how the site is hosted (their `CLAUDE.md` currently
  assumes Render, which is a different plan than "the Pi hosts it") and the HTTPS tunnel in front of it.

## Tests
- Unit: `RequestBudget`/`CharacterBudget` caps, `dotenv.py`'s loader, and `DecisionClient` against a fake HTTP
  server (magic type/level validation, malformed output, retry, timeout, and conversation turns),
  `TtsClient` against a fake ElevenLabs-shaped server, `DecisionService`'s budget/error/success paths,
  `ConversationSession` against a stub client (history accumulation, reaching done, rollback-on-error,
  raising if you `.respond()` after done), `EspLink` against a fake serial object, `AudioPlayer` against fake
  player commands, `TeammateClient` via direct `handle_message()` calls (session:start/transcript/session:end,
  budget exhaustion on both budgets, magic reveal/file-write without dispensing) and one real-websocket round-trip test.
- Still needed: a real integration test once the website contract addition and ESP32 are both built;
  `tools/smoke_pi.sh` (checks internet reachability and the env key *names*, never values, plus the serial
  device).

## Process
- Present a short plan first, then build incrementally, committing after each piece with a working test.
- Ask questions only if blocked. Otherwise choose sensible defaults, state them, and keep going.
- Keep the README current with Pi setup, the magic-assessment flow, tests, and hardware notes.

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
  it as data, and structured-output validation (magic type, level 1-10, no URL, length-capped) bounds what a
  successful injection could produce.
- **No audio pregeneration anymore.** Every reply is freshly LLM-written, so there's no cache hit path — every
  successful request pays a live TTS call's latency and cost, with no way around it now that pre-written lines
  aren't in the normal path.
