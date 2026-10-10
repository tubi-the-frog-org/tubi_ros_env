# Hosting the website on the Pi + wiring it to pi_app

## How the pipeline works

```
phone (HTTPS via tunnel) --POST /api/transcribe (audio)--> website/server (Node, on the Pi)
                                                              | stt_worker.py (Whisper) -> text
                                                              | appends output/transcripts.jsonl (log only)
                                                              v  WS push, only to ?role=teammate clients
                                                    pi_app.teammate_client  -> LLM -> TTS -> speaker / ESP32
```

pi_app does NOT poll `output/transcripts.jsonl` (the file is wiped at every session start/end, so it's only
a debugging log). The server pushes each transcript over the WebSocket the instant Whisper finishes, along
with `session:start` / `session:end`. Browsers never receive these.

## One-time setup on the Pi

```sh
sudo apt update && sudo apt install -y git nodejs npm mpg123 python3-venv
git clone <repo-url> ~/tubi_ros_env && cd ~/tubi_ros_env

cd website && npm install && npm run build            # no Python/Whisper/ffmpeg needed
cd ../pi_app && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp secrets.env.example secrets.env && nano secrets.env
```

Speech-to-text runs in the cloud (`/api/transcribe` forwards the audio to OpenAI using `STT_API_KEY`, falling
back to `LLM_API_KEY`), so there is no Whisper/torch on the Pi. Check `node -v` is >= 18.

## Public HTTPS (phones need it for the mic)

Quick tunnel, no domain or router config, works over the phone hotspot:

```sh
# arm64 binary from https://github.com/cloudflare/cloudflared/releases
sudo install cloudflared-linux-arm64 /usr/local/bin/cloudflared
cloudflared tunnel --url http://localhost:3000     # prints https://<random>.trycloudflare.com
```

The quick-tunnel URL changes on every restart, so the NFC sticker would need rewriting. For a stable URL use
a named tunnel on a domain you own (`cloudflared tunnel login/create/route dns`).

## Run it

Manual (good for the first test, three terminals):

```sh
cd ~/tubi_ros_env/pi_app && set -a && . ./secrets.env && set +a && cd ../website && NODE_ENV=production PORT=3000 node server/index.js
cd ~/tubi_ros_env/pi_app  && set -a && . ./secrets.env && set +a && .venv/bin/python -m pi_app.teammate_client
cloudflared tunnel --url http://localhost:3000
```

Autostart on boot:

```sh
sudo cp ~/tubi_ros_env/pi_app/deploy/tubi-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tubi-web tubi-brain tubi-tunnel
journalctl -u tubi-brain -f          # "connected to ws://localhost:3000/?role=teammate"
journalctl -u tubi-tunnel | grep trycloudflare
```

Edit the `User=` / paths in the units if the repo isn't at `/home/maxwelllokshin/tubi_ros_env`.

## Things to verify on real hardware

1. **STT latency/cost:** each press-and-hold is one cloud call; time a turn end to end.
2. The talk button is disabled while Tubi thinks/speaks (server-enforced: `/api/transcribe` returns 409 and
   the browser gets `tubi-state`). It unlocks when pi_app sends `state speaking:false`, when pi_app disconnects,
   or after `TUBI_TURN_TIMEOUT_MS` (60 s).
3. Audio plays from a speaker on the **Pi** (mpg123). The ESP32 firmware currently has no audio code.
4. `journalctl -u tubi-brain` shows LLM/TTS errors; budget caps are `LLM_DAILY_REQUEST_CAP` / `TTS_DAILY_CHAR_CAP`.
