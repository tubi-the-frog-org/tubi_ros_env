# Wizard snack animatronic — Pi-side ROS 2 workspace

## Dev environment (Mac)

All development and testing happens in a Docker container running ROS 2 Jazzy
(`ros:jazzy-ros-base`, native arm64 image), so nothing needs to be installed
on the Mac itself besides Docker.

```sh
docker compose build dev          # one-time (or after editing docker/Dockerfile)
docker compose run --rm dev bash  # opens a shell in /workspace/animatronic_ws
```

Inside the container:

```sh
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

The repo root is bind-mounted at `/workspace`, so edits made on the Mac are
immediately visible in the container and vice versa. `build/`, `install/`,
and `log/` under `animatronic_ws/` are colcon artifacts and are gitignored —
safe to delete any time.

This container is for Mac-side development only. Deployment to the Raspberry
Pi uses native ROS 2 Jazzy packages (not this Docker image) — see Step 0 in
`CLAUDE.md`.

## Repo layout

- `animatronic_ws/` — the Pi-side ROS 2 workspace (this task's scope).
- `esp32_firmware/` — PlatformIO project for the ESP32 (servos, later audio
  playback). Out of scope here; see `esp32_firmware/README.md`.

## Risks and recommendations (living list, updated as packages are built)

- **Hotspot dependence.** The Pi's only internet is a phone hotspot that can
  drop. The cloud LLM provider (brain) and the cloud TTS provider (speech)
  both need it; `keyword`/`fallback` plus pre-generated audio must keep
  working with zero network, within the configured timeout, so a dropped
  hotspot degrades the experience rather than hanging it.
- **Cloud API cost.** Both the brain's cloud LLM calls and speech's TTS
  calls cost money per request/character and this is a publicly reachable
  web endpoint. Mitigated by `cloud_daily_request_cap`/`cloud_total_request_cap`
  + the `cloud_enabled` kill switch (brain), and a per-day character cap
  (speech, not yet built) — but caps need setting to real values before
  this goes live publicly, not just left at defaults.
- **Key security.** `LLM_API_KEY`, `TTS_API_KEY`, `WEB_BRIDGE_TOKEN` come
  from env/`secrets.env` only (see `secrets.env.example`), never a ROS
  param (params are introspectable at runtime) and never logged above
  DEBUG. The browser (speech-to-text only) never receives or holds any of
  these.
- **Prompt injection.** The guest's spoken text reaches the cloud LLM as
  untrusted input. The system prompt explicitly frames it as data, not
  instructions, and the structured-output schema + reply validation (snack
  must be a real enum value, reply must mention that snack, no URLs, length
  capped) bound what a successful injection could even produce — worst case
  is a rejected response that falls through to the next provider.
