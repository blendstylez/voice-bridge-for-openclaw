# Jarvis Voice Loop — Technical Documentation

> **Note:** This documentation uses placeholder values like `<mac-mini-lan-ip>`
> and `<project-root>`. Replace them with values appropriate to your setup.

> **Version:** 3.0.1-pi  
> **Last updated:** 2026-04-15 (Doku-Review-Pass: IP-Fix, Section 6 Rotation, Health API)  
> **Author:** Maintained by Jarvis (bridge side) and Claude Code (client side)  
> **Project root (Mac mini):** `<project-root>/projects/Jarvis-remote-endpoint/`  
> **Project root (MBP):** `<project-root>/Projects/openWakeWorld/openWakeWord/`  
> **Project root (Pi):** `<project-root>/jarvis-voice/`

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Components](#3-components)
   - 3.1 [Voice Bridge (Mac mini)](#31-voice-bridge-mac-mini)
   - 3.2 [Voice Client (MBP)](#32-voice-client-mbp)
   - 3.3 [Playback Server (MBP)](#33-playback-server-mbp)
   - 3.4 [Voice Client (Pi)](#34-voice-client-pi)
   - 3.5 [Playback Server (Pi)](#35-playback-server-pi)
4. [Request Lifecycle](#4-request-lifecycle)
5. [Configuration Reference](#5-configuration-reference)
   - 5.1 [Bridge .env (Mac mini)](#51-bridge-env-mac-mini)
   - 5.2 [Client .env (MBP)](#52-client-env-mbp)
   - 5.3 [Client .env (Pi)](#53-client-env-pi)
   - 5.4 [Hardcoded Values in voice_bridge.py](#54-hardcoded-values-in-voice_bridgepy)
6. [Secrets & Credentials](#6-secrets--credentials)
7. [Network Topology](#7-network-topology)
8. [Process Management](#8-process-management)
9. [Logging](#9-logging)
10. [HTTP API Reference](#10-http-api-reference)
11. [External Services](#11-external-services)
12. [Testing](#12-testing)
13. [Troubleshooting](#13-troubleshooting)
14. [File Inventory](#14-file-inventory)
15. [Known Limitations & Future Work](#15-known-limitations--future-work)
16. [MBP Aliases for Pi Maintenance](#16-mbp-aliases-for-pi-maintenance)

---

## 1. System Overview

The Jarvis Voice Loop provides a voice interface to Jarvis, the homelab AI agent running on a Mac mini via OpenClaw. A user speaks into a microphone on a client device (MacBook Pro or Raspberry Pi), and Jarvis responds through the speaker of that same device — similar to a smart assistant, but powered by a full-capability AI agent that can delegate, run tools, and take its time.

**Multi-client:** As of v3, two independent listening posts run simultaneously — the MBP and a Raspberry Pi. Each device has its own playback server; the Bridge routes replies back to the correct device based on the `source` field in the upload.

**Key design principle:** The client and OpenClaw are **fully decoupled**. The client fires audio and immediately returns to listening. The agent processes asynchronously and pushes the reply back when ready. No blocking waits, no HTTP timeouts on long agent turns.

---

## 2. Architecture

```
Client MBP (<mbp-lan-ip>)  ─┐
  [A] voice_client.py         │  POST /voice (multipart: audio + source)
  [C] playback_server :18780  │◀── POST /play (WAV reply)
                               │
                               ├──▶ [B] voice_bridge.py (Mac mini :18790)
                               │        Groq Whisper STT → transcript
Client Pi  (<pi-lan-ip>)  ─┘        Returns 202 Accepted immediately
  [A] voice_client.py                  ↓ (background thread)
  [C] playback_server :18780           OpenClaw Chat API → agent reply
                                        ElevenLabs TTS → WAV
                                        CLIENT_ROUTES lookup by source
                                        POST /play → correct client
```

**Reply routing:** Each client sends `source=mbp-dev` or `source=pi-livingroom` with the upload. The Bridge uses `CLIENT_ROUTES` to find the right playback server IP and sends the reply only there. No cross-talk between devices.

**Two independent HTTP round-trips, both non-blocking from the caller's perspective:**

1. **Client → Bridge:** Upload audio, receive 202 Accepted, done.
2. **Bridge → Client:** Push reply WAV, receive 200, done.

---

## 3. Components

### 3.1 Voice Bridge (Mac mini)

**File:** `<project-root>/projects/Jarvis-remote-endpoint/voice_bridge.py`  
**Host:** Mac mini (<mac-mini-hostname>, Tailscale: `<mac-mini-tailscale-ip>`, LAN: `<mac-mini-lan-ip>`)  
**Port:** `18790`  
**WSGI Server:** Waitress (4 threads), fallback to Flask dev server  
**LaunchAgent:** `ai.openclaw.voice-bridge`  
**Python venv:** `<project-root>/projects/Jarvis-remote-endpoint/.venv/`

**Responsibilities:**
- Receives audio uploads from voice clients via `POST /voice`
- Authenticates requests via Bearer token
- Validates that `source` field is present and maps to a known client route
- Runs Groq Whisper STT (synchronous, ~300-500ms)
- Returns `202 Accepted` with transcript to the client
- Spawns a background thread that:
  - Sends transcript to OpenClaw Chat Completions API (blocking, up to 120s)
  - Converts agent reply to speech via ElevenLabs TTS
  - POSTs the resulting WAV to the client's playback server
- Exposes `GET /health` for monitoring

**Dependencies (requirements.txt):**
```
flask
requests
python-dotenv
waitress
```

### 3.2 Voice Client (MBP)

**File:** `<project-root>/Projects/openWakeWorld/openWakeWord/jarvis_voice_client.py`  
**Host:** MacBook Pro (<mbp-hostname>, Tailscale: `<mbp-tailscale-ip>`, LAN: `<mbp-lan-ip>`)  
**Version:** async v2 (updated 2026-04-13)  
**Python venv:** `<project-root>/Projects/openWakeWorld/openWakeWord/venv/` (Python 3.9)  
**Built by:** Claude Code

**Responsibilities:**
- Runs continuous wake word detection using openWakeWord (`hey_jarvis` ONNX model, threshold 0.5)
- Records audio until silence (2.0s of RMS < 400) or hard cap of 10 seconds
- Saves recorded WAV to `/tmp/jarvis/jarvis_turn_<timestamp>.wav`
- Uploads WAV to Bridge via `POST /voice` (multipart: `audio` + `source` fields)
- Expects `202 Accepted` within 10 seconds, logs it, does **not** wait for the reply
- Immediately returns to `[LISTENING]` state — reply arrives later via `playback_server.py`
- Resets openWakeWord prediction buffer after each turn (prevents re-trigger from own voice)

**Status cycle:** `[LISTENING]` → `[WAKE DETECTED]` → `[RECORDING]` → `[SENDING]` → `[UPLOADED]` → `[LISTENING]`

**Key constants:**

| Constant | Value | Purpose |
|---|---|---|
| `WAKE_THRESHOLD` | `0.5` | Minimum openWakeWord score to trigger |
| `SAMPLE_RATE` | `16000` Hz | Whisper-compatible audio |
| `CHUNK` | `1280` samples | ~80ms per frame |
| `RECORD_MAX_SECONDS` | `10` | Hard cap on recording length |
| `SILENCE_SECONDS` | `2.0` | Consecutive silence before stopping |
| `SILENCE_RMS_THRESH` | `400` | RMS below this = silence (calibrated for the user's room) |
| `HTTP_TIMEOUT` | `10` s | Timeout for upload 202-ack (not agent processing) |

**Audio pipeline:**
1. PyAudio reads 1280-sample chunks (paInt16, mono, 16kHz)
2. openWakeWord runs ONNX inference on each chunk
3. On wake: drain available buffer from mic (prefix audio), begin recording
4. RMS silence detection per chunk during recording
5. `wave` module writes PCM to WAV file (16-bit, mono, 16kHz)
6. `requests` POSTs multipart to Bridge

**Dependencies:**
- `pyaudio` — microphone capture
- `numpy` — ONNX inference input format
- `openwakeword` — wake word model (ONNX, `hey_jarvis`)
- `requests` — HTTP upload
- `python-dotenv` — `.env` loading

**Error handling:** Network errors and timeouts are caught and logged; script returns to `[LISTENING]` regardless.

### 3.3 Playback Server (MBP)

**File:** `<project-root>/Projects/openWakeWorld/openWakeWord/playback_server.py`  
**Host:** MacBook Pro (same as Voice Client)  
**Port:** `18780`  
**WSGI Server:** Flask built-in dev server (sufficient for single-connection use case)  
**LaunchAgent:** `com.jarvis.playback-server`  
**Version:** 1.0.0 (created 2026-04-13)  
**Built by:** Claude Code

**Responsibilities:**
- Receives `POST /play` with raw WAV body (or multipart) from the Bridge
- Authenticates via Bearer token (`PLAYBACK_BEARER_TOKEN`)
- Saves WAV to `/tmp/jarvis/playback_<timestamp>.wav`
- Spawns `afplay` as non-blocking subprocess (`Popen`) — returns `200` immediately
- Exposes `GET /health` for monitoring
- Binds to `0.0.0.0` so it is reachable from the Mac mini

**Why non-blocking Popen?** The playback call from the Bridge is itself fire-and-forget. If `afplay` blocked the HTTP handler, the Bridge would hang waiting. The Bridge doesn't care when audio finishes — just that delivery was accepted.

**Accepted input formats:**

| Content-Type | How body is read |
|---|---|
| `audio/wav` (raw) | `request.data` |
| `multipart/form-data` | `request.files["audio"].read()` |

**Dependencies:**
- `flask` — HTTP server
- `python-dotenv` — `.env` loading
- `afplay` — macOS system audio player (built-in, no install needed)

### 3.4 Voice Client (Pi)

**File:** `<project-root>/jarvis-voice/jarvis_voice_client.py`  
**Host:** Raspberry Pi (`<pi-hostname>`, LAN: `<pi-lan-ip>`)  
**Python venv:** `<project-root>/jarvis-voice/venv/` (Python 3.13.5)  
**Process manager:** systemd user service `jarvis-voice-client.service`  
**Built by:** Claude Code (2026-04-15, Phase B)

**Responsibilities:** Identical to MBP client — wake word detection, recording, async upload. Behavior and HTTP API usage are the same. Differences are purely in the audio hardware layer.

**Key differences from MBP client:**

| Aspect | MBP | Pi |
|---|---|---|
| Mic device | CoreAudio default | USB ENC Audio Device `hw:2,0`, PyAudio index=1 |
| PyAudio open rate | 16000 Hz mono | **48000 Hz stereo** (hardware constraint) |
| Audio conversion | None needed | `convert_native_to_16k_mono()`: stereo avg + 3:1 decimation |
| openWakeWord model | Local dev source (auto-download) | Bundled pip ONNX: `resources/models/hey_jarvis_v0.1.onnx` |
| `CLIENT_SOURCE_NAME` | `mbp-dev` | `pi-livingroom` |

**Audio pipeline (Pi-specific):**
1. PyAudio reads `CHUNK_NATIVE=3840` samples at 48kHz stereo (80ms of audio, 15360 bytes)
2. `convert_native_to_16k_mono()`: average left+right channels → mono, then decimate every 3rd sample → 1280 samples at 16kHz
3. openWakeWord runs ONNX inference on the 1280-sample int16 array
4. On wake: same recording / upload flow as MBP
5. All saved WAVs are correctly 16kHz mono S16LE

**Important notes:**

- **Device index stability:** PyAudio index=1 for the USB mic is valid **only when no other process holds the device**. While the service is running, a second PyAudio session in a shell will not see the mic in the enumeration — this is normal ALSA `hw:` behavior.
- **Do NOT use `~/.asoundrc` with `type asym`** on this device — it breaks PyAudio's device enumeration entirely (the USB mic disappears from the device list). Root cause: PortAudio's ALSA backend enumerates devices via `snd_device_name_hint()` and then probes each device with `snd_pcm_open()` to read its capabilities. With `type asym`, the "default" PCM exposes a composite device whose capture and playback sub-trees are separate ALSA slaves. PortAudio's probing logic doesn't handle this split correctly — it queries the asym wrapper and receives `maxInputChannels=0` for all devices, effectively hiding the capture-capable USB mic. The `plug` sub-device over `hw:2,0` is simply not surfaced as an independently enumerable input device through the asym indirection.
- **Startup time:** First `[LISTENING]` log line appears ~60–90 seconds after service start (ONNX runtime init on ARM64 is slow). The service shows `active (running)` during this time — it is not frozen.
- **`PYTHONUNBUFFERED=1`** is set in the systemd service file — required for print() output to reach the log file without manual flush.

**Dependencies (pip, `venv/`):**
- `pyaudio` — microphone capture (requires `portaudio19-dev` system package)
- `numpy` — stereo→mono conversion and downsampling
- `openwakeword==0.5.1` (installed `--no-deps`, tflite-runtime unavailable on Python 3.13/ARM64)
- `onnxruntime` — ONNX inference backend
- `scipy`, `scikit-learn`, `tqdm` — openwakeword transitive deps
- `requests` — HTTP upload
- `python-dotenv` — `.env` loading
- `flask` — used by playback_server (not this script)

### 3.5 Playback Server (Pi)

**File:** `<project-root>/jarvis-voice/playback_server.py`  
**Host:** Raspberry Pi (same as Voice Client)  
**Port:** `18780`  
**Process manager:** systemd user service `jarvis-playback.service`

**Responsibilities:** Identical to MBP playback server. Only difference: uses `aplay` (ALSA) instead of `afplay` (macOS).

**Pi-specific playback call:**
```python
ALSA_DEVICE = os.getenv("ALSA_PLAYBACK_DEVICE", "plughw:3,0")
subprocess.Popen(["aplay", "-q", "-D", ALSA_DEVICE, wav_path])
```

- `-q` suppresses aplay's per-play stderr output
- `-D plughw:3,0` targets the USB speaker (card 3) via the ALSA plug layer (handles format conversion)
- The device string is configurable via `ALSA_PLAYBACK_DEVICE` in `.env`

---

## 4. Request Lifecycle

A complete voice interaction flows through these stages, each logged with a phase label:

```
Time   Phase             Where           What happens
─────  ────────────────  ──────────────  ──────────────────────────────────────
 0ms   [WAKE]            MBP Client      Wake word "Hey Jarvis" detected
 0ms   [RECORDING]       MBP Client      Microphone recording starts
 ~3s   [UPLOADED]        MBP Client      WAV uploaded to Bridge, 202 received
       [LISTENING]       MBP Client      Client returns to listening immediately
 ~3s   [REQUEST]         Mac mini Bridge  Audio received, auth + validation OK
 ~3s   [STT]             Mac mini Bridge  Groq Whisper transcribes audio (~350ms)
       ← 202 returned →  
 ~3.4s [AGENT]           Mac mini Bridge  Transcript sent to OpenClaw Chat API (background thread)
 ~8-15s [AGENT] Done     Mac mini Bridge  CEO agent reply received
 ~8-15s [TTS]            Mac mini Bridge  ElevenLabs synthesizes reply (~1-4s)
 ~12s  [PLAYBACK-POST]   Mac mini Bridge  WAV pushed to MBP playback server (~30ms)
 ~12s  [PLAY]            MBP Playback     Audio plays through speakers
 ~12s  [COMPLETE]        Mac mini Bridge  Full timing summary logged
```

**Typical end-to-end latency:** 6-12 seconds (from upload to audio playback).

---

## 5. Configuration Reference

### 5.1 Bridge .env (Mac mini)

**File:** `<project-root>/projects/Jarvis-remote-endpoint/.env`

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `BRIDGE_PORT` | HTTP port for the bridge server | `18790` | No |
| `BRIDGE_TOKEN` | Bearer token for authenticating incoming `/voice` requests | — | **Yes** |
| `GROQ_API_KEY` | Groq API key for Whisper STT | — | **Yes** |
| `GROQ_BASE_URL` | Groq API base URL | `https://api.groq.com/openai/v1` | No |
| `OPENCLAW_API_URL` | OpenClaw Chat Completions endpoint | `http://127.0.0.1:18789/v1/chat/completions` | No |
| `OPENCLAW_API_TOKEN` | OpenClaw Gateway auth token | — | **Yes** |
| `OPENCLAW_MODEL` | Which OpenClaw agent to route to | `openclaw/ceo` | No |
| `OPENCLAW_USER` | Stable user ID for session persistence | `voice-client` | No |
| `ELEVENLABS_API_KEY` | ElevenLabs API key for TTS | — | **Yes** |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice to use for synthesis | `EXAVITQu4vr4xnSDxMaL` (Sarah) | No |
| `AGENT_TIMEOUT_SECONDS` | Max wait time for agent reply before dropping | `120` | No |
| `CLIENT_ROUTES` | JSON map of source names to playback URLs | `{}` | **Yes** (for replies to work) |
| `CLIENT_PLAYBACK_TOKEN` | Bearer token for authenticating to client playback servers | — | No (but recommended) |

**Example .env:**
```env
BRIDGE_PORT=18790
BRIDGE_TOKEN=<your-bridge-token>

GROQ_API_KEY=gsk_xxxxx
GROQ_BASE_URL=https://api.groq.com/openai/v1

OPENCLAW_API_URL=http://127.0.0.1:18789/v1/chat/completions
OPENCLAW_API_TOKEN=<your-openclaw-gateway-token>
OPENCLAW_MODEL=openclaw/ceo
OPENCLAW_USER=voice-client

ELEVENLABS_API_KEY=sk_xxxxx
ELEVENLABS_VOICE_ID=<elevenlabs-voice-id>

AGENT_TIMEOUT_SECONDS=120

CLIENT_ROUTES={"mbp-dev":"http://<mbp-tailscale-ip>:18780/play","pi-livingroom":"http://<pi-lan-ip>:18780/play"}
CLIENT_PLAYBACK_TOKEN=<your-playback-token>
```

> **Note on MBP vs Pi routing:** The MBP route uses its Tailscale IP (`<mbp-tailscale-ip>`), while the Pi route uses its LAN IP (`<pi-lan-ip>`). The Pi has no Tailscale. Both work from the Mac mini. The Mac mini's LAN IP is `<mac-mini-lan-ip>` (subnet `.2.x`), the Pi is on `.1.x` — cross-subnet routing works via the router but adds ~300ms latency vs. same-subnet (~2ms). Not critical for TTS playback delivery.

### 5.2 Client .env (MBP)

**File:** `<project-root>/Projects/openWakeWorld/openWakeWord/.env`  
**Note:** Uses `export` syntax (can be sourced directly in shell or read by python-dotenv).

| Variable | Purpose | Example |
|---|---|---|
| `OPENCLAW_WEBHOOK_URL` | Full Bridge endpoint URL | `http://<mac-mini-lan-ip>:18790/voice` |
| `OPENCLAW_BEARER_TOKEN` | Same as `BRIDGE_TOKEN` on the Bridge side | `MoXIWcUZngf_...` |
| `CLIENT_SOURCE_NAME` | Source identifier sent with each upload | `mbp-dev` |
| `PLAYBACK_PORT` | Port the playback server listens on | `18780` |
| `PLAYBACK_BEARER_TOKEN` | Same as `CLIENT_PLAYBACK_TOKEN` on Bridge side | `MoXIWcUZngf_...` |

> **Note on env var name:** `voice_client.py` reads `OPENCLAW_WEBHOOK_URL` (not `OPENCLAW_VOICE_URL`). The project spec used `OPENCLAW_VOICE_URL` as a name but the actual implementation uses `OPENCLAW_WEBHOOK_URL`. Both refer to the same endpoint. Do not rename — changing it requires updating `.env` and the client script together.

**Current .env (sanitized):**
```env
export OPENCLAW_WEBHOOK_URL="http://<mac-mini-lan-ip>:18790/voice"
export OPENCLAW_BEARER_TOKEN="<bridge-token>"
export CLIENT_SOURCE_NAME="mbp-dev"

# Playback server (playback_server.py)
export PLAYBACK_PORT="18780"
export PLAYBACK_BEARER_TOKEN="<playback-token>"
```

### 5.3 Client .env (Pi)

**File:** `<project-root>/jarvis-voice/.env`

| Variable | Purpose | Pi value |
|---|---|---|
| `OPENCLAW_WEBHOOK_URL` | Bridge endpoint | `http://<mac-mini-lan-ip>:18790/voice` |
| `OPENCLAW_BEARER_TOKEN` | Same as `BRIDGE_TOKEN` | (shared with MBP) |
| `CLIENT_SOURCE_NAME` | Routing key sent with upload | `pi-livingroom` |
| `PLAYBACK_PORT` | Port playback server listens on | `18780` |
| `PLAYBACK_BEARER_TOKEN` | Same as `CLIENT_PLAYBACK_TOKEN` | (shared with MBP) |
| `INPUT_DEVICE_INDEX` | PyAudio device index for USB mic | `1` |
| `ALSA_PLAYBACK_DEVICE` | ALSA device string for speaker | `plughw:3,0` |

> **Note on `INPUT_DEVICE_INDEX`:** This index was determined once via `pyaudio.PyAudio().get_device_info_by_index()` enumeration. It can shift if USB devices are plugged/unplugged in different order at boot. If the voice client fails to open the mic after a reboot, run `venv/bin/python3 -c "import pyaudio; p=pyaudio.PyAudio(); [print(i, p.get_device_info_by_index(i)['name'], p.get_device_info_by_index(i)['maxInputChannels']) for i in range(p.get_device_count())]"` (with service stopped first) and update the `.env`.

### 5.4 Hardcoded Values in voice_bridge.py

These values are **not** configurable via .env and require a code change + restart:

| Value | Location in code | Current value | Purpose |
|---|---|---|---|
| `GROQ_TRANSCRIPTION_MODEL` | Top-level constant | `"whisper-large-v3"` | Groq STT model |
| `ELEVENLABS_MODEL_ID` | Top-level constant | `"eleven_v3"` | ElevenLabs TTS model |
| `ELEVENLABS_OUTPUT_FORMAT` | Top-level constant | `"pcm_16000"` | TTS output format |
| `ELEVENLABS_BASE_URL` | Top-level constant | `"https://api.elevenlabs.io/v1"` | ElevenLabs API base |
| `PCM_SAMPLE_RATE` | Top-level constant | `16_000` | Audio sample rate (Hz) |
| `PCM_CHANNELS` | Top-level constant | `1` | Mono audio |
| `PCM_SAMPLE_WIDTH_BYTES` | Top-level constant | `2` | 16-bit samples |
| `STT_TIMEOUT_SECONDS` | Top-level constant | `60` | Groq API timeout |
| `TTS_TIMEOUT_SECONDS` | Top-level constant | `60` | ElevenLabs API timeout |
| `LOG_TEXT_PREVIEW_LENGTH` | Top-level constant | `120` | Max chars in log previews |
| Waitress thread count | `__main__` block | `4` | WSGI server threads |
| Playback POST timeout | `post_to_client()` | `30` (seconds) | Timeout for pushing WAV to client |

---

## 6. Secrets & Credentials

**All secrets live in the Bridge `.env` file.** No secrets are hardcoded in source code.

| Secret | Where stored | Where used | How to rotate |
|---|---|---|---|
| `BRIDGE_TOKEN` | Bridge `.env` + Client `.env` | Auth for `/voice` endpoint | Change in both .env files, restart both services |
| `GROQ_API_KEY` | Bridge `.env` | Groq Whisper STT API calls | Replace in Bridge `.env`, restart Bridge |
| `OPENCLAW_API_TOKEN` | Bridge `.env` | OpenClaw Chat Completions API | This is the **Gateway auth token** (`gateway.auth.token`) from `~/.openclaw/openclaw.json`. **Rotated 2026-04-15** after Security Audit changed it. When rotating: update Bridge `.env`, restart Bridge. Symptom of stale token: `[AGENT] FAILED | OpenClaw HTTP 401` |
| `ELEVENLABS_API_KEY` | Bridge `.env` | ElevenLabs TTS API calls | Replace in Bridge `.env`, restart Bridge |
| `CLIENT_PLAYBACK_TOKEN` | Bridge `.env` + Client `.env` | Auth for `/play` endpoint on client | Change in both .env files, restart both services |

**Important:** The `GROQ_API_KEY` is also stored in `~/.zshenv` on the Mac mini (added 2026-04-12 when the Groq account was created). The Bridge reads it from `.env`, not from `.zshenv`.

**OpenClaw Gateway token:** The `OPENCLAW_API_TOKEN` is the Gateway HTTP auth token. It was enabled when `gateway.http.endpoints.chatCompletions.enabled` was set to `true` in `openclaw.json`. This is **not** the same as the hooks token used for `/hooks/agent`.

### Token Rotation Procedures

**`BRIDGE_TOKEN` / `CLIENT_PLAYBACK_TOKEN`** (shared between Bridge and clients):
1. Generate a new token (e.g. `openssl rand -base64 32`).
2. Update `BRIDGE_TOKEN` in Bridge `.env` (`<project-root>/projects/Jarvis-remote-endpoint/.env`).
3. Update `OPENCLAW_BEARER_TOKEN` in MBP `.env` (`<project-root>/Projects/openWakeWorld/openWakeWord/.env`).
4. Update `OPENCLAW_BEARER_TOKEN` in Pi `.env` (`<project-root>/jarvis-voice/.env` via SSH).
5. Restart Bridge: `launchctl unload && load ~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist`.
6. Restart Pi services: `ssh <pi-hostname> 'systemctl --user restart jarvis-voice-client.service'`.
7. Same applies to `CLIENT_PLAYBACK_TOKEN` / `PLAYBACK_BEARER_TOKEN`.

**`OPENCLAW_API_TOKEN`** (Gateway auth token — rotates when OpenClaw config changes):
1. Read new token: `cat ~/.openclaw/openclaw.json | python3 -c "import sys,json; print(json.load(sys.stdin)['gateway']['auth']['token'])"`
2. Update `OPENCLAW_API_TOKEN` in Bridge `.env`.
3. Restart Bridge: `launchctl unload && load ~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist`.
4. No client-side change needed (clients don't talk to OpenClaw directly).
5. **Symptom of stale token:** `[AGENT] FAILED | OpenClaw HTTP 401` in Bridge logs.
6. **Common trigger:** OpenClaw updates, security audits, or manual `openclaw.json` edits can rotate this token without warning.

**`GROQ_API_KEY` / `ELEVENLABS_API_KEY` / `GROK_API_KEY`** (external API keys):
1. Replace in Bridge `.env` only.
2. Restart Bridge.
3. No client-side change needed.

---

## 7. Network Topology

All communication happens over LAN (192.168.x.x). Tailscale (100.x.x.x) also works for the MBP↔Mac mini path; the Pi communicates LAN-only.

```
┌──────────────────────────────────┐
│  Mac mini (<mac-mini-hostname>)           │
│  LAN: <mac-mini-lan-ip>              │◀── POST /voice (MBP + Pi)
│                                  │──▶ POST /play  (MBP or Pi, by source)
│  ┌────────────────────────┐     │
│  │ voice_bridge.py :18790 │     │
│  │  ├─ Groq API (ext)     │     │
│  │  ├─ OpenClaw :18789    │     │
│  │  └─ ElevenLabs (ext)   │     │
│  └────────────────────────┘     │
│  ┌────────────────────────┐     │
│  │ OpenClaw Gateway :18789│     │
│  └────────────────────────┘     │
└──────────────────────────────────┘
         ▲  POST /voice          ▲  POST /voice
         │  source=mbp-dev       │  source=pi-livingroom
         │                       │
┌────────┴─────────┐    ┌────────┴─────────┐
│  MacBook Pro     │    │  Raspberry Pi    │
│  LAN: .1.217     │    │  LAN: .1.119     │
│                  │    │                  │
│ voice_client.py  │    │ voice_client.py  │
│ playback  :18780 │    │ playback  :18780 │
│ afplay           │    │ aplay -D plughw  │
└──────────────────┘    └──────────────────┘

External APIs:
  → Groq (api.groq.com)              STT
  → ElevenLabs (api.elevenlabs.io)   TTS
```

**Ports summary:**

| Port | Host | Service | Direction |
|---|---|---|---|
| `18790` | Mac mini | Voice Bridge (inbound audio) | Client → Bridge |
| `18789` | Mac mini | OpenClaw Gateway (Chat API) | Bridge → OpenClaw (localhost) |
| `18780` | MBP | Playback Server (reply audio) | Bridge → MBP |
| `18780` | Pi | Playback Server (reply audio) | Bridge → Pi |

> **Firewall note (Pi):** If `ufw` is active on the Pi, allow traffic from your LAN CIDR to port `18780`, for example: `sudo ufw allow from <lan-cidr> to any port 18780`

---

## 8. Process Management

### Bridge (Mac mini)

**Plist:** `~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.openclaw.voice-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string><project-root>/projects/Jarvis-remote-endpoint/.venv/bin/python3</string>
    <string>voice_bridge.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string><project-root>/projects/Jarvis-remote-endpoint</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/openclaw/voice-bridge-stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/openclaw/voice-bridge-stderr.log</string>
</dict>
</plist>
```

**Management commands:**
```bash
# Check status
launchctl list | grep voice-bridge

# Restart
launchctl unload ~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist
launchctl load ~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist

# View logs
tail -f /tmp/openclaw/voice-bridge-stderr.log
```

### Playback Server (MBP)

**Plist:** `~/Library/LaunchAgents/com.jarvis.playback-server.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jarvis.playback-server</string>
    <key>ProgramArguments</key>
    <array>
        <string><project-root>/Projects/openWakeWorld/openWakeWord/venv/bin/python3</string>
        <string><project-root>/Projects/openWakeWorld/openWakeWord/playback_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string><project-root>/Projects/openWakeWorld/openWakeWord</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/jarvis/playback_server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/jarvis/playback_server.err</string>
</dict>
</plist>
```

> **Note:** Uses the venv Python (`venv/bin/python3`, Python 3.9) — not the system `/usr/bin/python3` — because Flask is installed in the venv.  
> **Note:** `/tmp/jarvis/` must exist before first start. The playback_server.py creates it at startup (`os.makedirs`), but if LaunchAgent starts before the script runs for the first time, log redirect may fail silently. Ensure it exists: `mkdir -p /tmp/jarvis`.

**Management commands (run on MBP):**
```bash
# First-time load (start on login enabled)
launchctl load ~/Library/LaunchAgents/com.jarvis.playback-server.plist

# Check status
launchctl list | grep playback

# Restart after code change
launchctl unload ~/Library/LaunchAgents/com.jarvis.playback-server.plist
launchctl load ~/Library/LaunchAgents/com.jarvis.playback-server.plist

# View logs
tail -f /tmp/jarvis/playback_server.log
tail -f /tmp/jarvis/playback_server.err
```

### Voice Client (MBP)

The voice client (`jarvis_voice_client.py`) is **not** managed by a LaunchAgent — it is started manually in a terminal session. This is intentional: it requires microphone access and direct TTY output for status monitoring.

```bash
cd <project-root>/Projects/openWakeWorld/openWakeWord
source venv/bin/activate   # or: source .env
python3 jarvis_voice_client.py
```

### Pi Services (systemd --user)

Both Pi scripts run as **systemd user services** under the `jarvis` user, with linger enabled (services survive SSH logout and start on boot without a login session).

**Linger (one-time setup, already done):**
```bash
sudo loginctl enable-linger jarvis
```

**Service files:** `~/.config/systemd/user/jarvis-playback.service` and `jarvis-voice-client.service`

Both service files set `PYTHONUNBUFFERED=1` so Python print() output is written to log files without buffering.

**Management commands (run on Pi or via `ssh <pi-hostname>`):**
```bash
# Status
systemctl --user status jarvis-playback.service
systemctl --user status jarvis-voice-client.service

# Restart after code or .env changes
systemctl --user restart jarvis-playback.service
systemctl --user restart jarvis-voice-client.service

# View live logs
tail -f <project-root>/jarvis-voice/logs/voice-client.log
tail -f <project-root>/jarvis-voice/logs/voice-client.err
tail -f <project-root>/jarvis-voice/logs/playback.log

# Full journal (if available)
journalctl --user -u jarvis-voice-client.service -f
```

**Key service properties:**

| Property | playback | voice-client |
|---|---|---|
| `After=` | `network-online.target sound.target` | `+ jarvis-playback.service` |
| `Restart=` | `on-failure` | `on-failure` |
| `RestartSec=` | 5s | 10s |
| `PYTHONUNBUFFERED` | 1 | 1 |

> **Note:** The `jarvis-voice-client.service` waits for `jarvis-playback.service` to start first (`After=`), but does **not** require it to be healthy — both can run independently.

> **Note:** After a reboot, expect ~60–90 seconds before `[LISTENING]` appears in the voice-client log. The service is running and healthy during this time (ONNX runtime init on ARM64 is slow).

---

## 9. Logging

### Bridge (Mac mini)

All Bridge logs go to **stderr** (captured by LaunchAgent at `/tmp/openclaw/voice-bridge-stderr.log`).

### Voice Client (MBP)

Logs to **stdout** (terminal). No log file — run interactively.

**Status labels:**

| Label | Meaning |
|---|---|
| `[LISTENING]` | Idle, openWakeWord scanning mic |
| `[WAKE DETECTED]` | Wake word scored above threshold |
| `[RECORDING]` | Actively recording user speech |
| `[RECORDING] silence detected` | Silence threshold triggered early stop |
| `[RECORDING] saved N chunks → path` | WAV written to disk |
| `[SENDING]` | HTTP POST to Bridge started |
| `[UPLOADED] 202 Accepted` | Bridge acknowledged, background processing started |
| `[ERROR] ...` | Network error, timeout, or bad HTTP status |
| `[EXIT]` | Ctrl+C received, shutting down |

### Voice Client & Playback Server (Pi)

Both services write to append-mode log files (configured in the systemd service files):

| File | Content |
|---|---|
| `<project-root>/jarvis-voice/logs/voice-client.log` | stdout: `[INIT]`, `[LISTENING]`, `[WAKE DETECTED]`, `[RECORDING]`, `[UPLOADED]`, `[ERROR]` |
| `<project-root>/jarvis-voice/logs/voice-client.err` | stderr: ALSA/JACK noise (ignorable), Python tracebacks (real errors) |
| `<project-root>/jarvis-voice/logs/playback.log` | stdout: `[INIT]`, `[PLAY]` lines |
| `<project-root>/jarvis-voice/logs/playback.err` | stderr: Flask errors |

**Filtering the ALSA noise from voice-client.err:**
```bash
grep -v 'ALSA\|jack\|Cannot connect\|JackShm\|DiscoverDevice\|GPU device\|drm\|sys/class' \
  <project-root>/jarvis-voice/logs/voice-client.err
```

**Temp WAV files:** Accumulate in `/tmp/jarvis/` on the Pi (same as MBP). Clean manually: `rm /tmp/jarvis/*.wav`

**Log rotation:** Log files are appended indefinitely — there is no automatic rotation (no logrotate config, no systemd timer). Use the MBP aliases from §16: `<pi-hostname>-clean` clears temp WAVs, `<pi-hostname>-cleanlogs` truncates all four log files. Automated rotation via logrotate or a systemd timer is Future Work (see §15).

### Playback Server (MBP)

Logs to **stdout/stderr**, captured by LaunchAgent:

| File | Content |
|---|---|
| `/tmp/jarvis/playback_server.log` | stdout (Flask startup, `[PLAY]` lines) |
| `/tmp/jarvis/playback_server.err` | stderr (Flask errors, auth failures) |

**Log format (stdout):**
```
[INIT] Jarvis playback server on 0.0.0.0:18780
[PLAY] 53804 bytes → /tmp/jarvis/playback_20260413_201305_123456.wav
127.0.0.1 - - [13/Apr/2026 20:13:05] "POST /play HTTP/1.1" 200 -
```

**Temp WAV files:** Each received audio is saved to `/tmp/jarvis/playback_<YYYYMMDD_HHMMSS_μs>.wav`. These accumulate over time — they are not cleaned up automatically. Periodic manual cleanup: `rm /tmp/jarvis/playback_*.wav`.

The recorded user speech is also saved: `/tmp/jarvis/jarvis_turn_<timestamp>.wav`. Same cleanup applies.

**Format:** `YYYY-MM-DD HH:MM:SS,mmm | LEVEL | message`

**Phase labels** — every log line in a request pipeline is prefixed with the current stage:

| Label | Stage | Meaning |
|---|---|---|
| `[REQUEST]` | Inbound | New audio received, validation passed |
| `[STT]` | Transcription | Groq Whisper start/done/fail |
| `[AGENT]` | Agent turn | OpenClaw Chat API start/done/fail |
| `[TTS]` | Synthesis | ElevenLabs start/done/fail |
| `[PLAYBACK-POST]` | Reply delivery | POST to client start/done/fail |
| `[COMPLETE]` | Summary | Full timing breakdown for the request |
| `[BACKGROUND]` | Error catch-all | Unexpected error in background thread |

**Request IDs:** Every request gets a sequential ID (`voice-0001`, `voice-0002`, ...) for log correlation. The counter resets on service restart.

**Example successful request:**
```
2026-04-13 20:13:06,665 | INFO | [REQUEST] req=voice-0001 | source=mbp-dev | filename=audio.wav | bytes=161324
2026-04-13 20:13:06,665 | INFO | [STT] Starting | req=voice-0001 | source=mbp-dev
2026-04-13 20:13:07,030 | INFO | [STT] Done | req=voice-0001 | source=mbp-dev | 365ms | transcript="Das ist ein Test."
2026-04-13 20:13:07,030 | INFO | [AGENT] Starting | req=voice-0001 | source=mbp-dev
2026-04-13 20:13:12,178 | INFO | [AGENT] Done | req=voice-0001 | source=mbp-dev | 5147ms | reply="Ja. Kommt sauber an."
2026-04-13 20:13:12,178 | INFO | [TTS] Starting | req=voice-0001 | source=mbp-dev
2026-04-13 20:13:13,022 | INFO | [TTS] Done | req=voice-0001 | source=mbp-dev | 843ms | wav_bytes=53804
2026-04-13 20:13:13,022 | INFO | [PLAYBACK-POST] Sending to client | req=voice-0001 | source=mbp-dev
2026-04-13 20:13:13,055 | INFO | [PLAYBACK-POST] Done | req=voice-0001 | source=mbp-dev | 33ms
2026-04-13 20:13:13,055 | INFO | [COMPLETE] req=voice-0001 | source=mbp-dev | agent=5147ms tts=843ms post=33ms total=6023ms
```

---

## 10. HTTP API Reference

### Bridge Endpoints (Mac mini :18790)

#### `POST /voice`

Upload audio for processing. Returns 202 immediately after transcription.

**Auth:** `Authorization: Bearer <BRIDGE_TOKEN>`  
**Content-Type:** `multipart/form-data`

**Form fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `audio` | file | **Yes** | WAV audio file |
| `source` | string | **Yes** | Client identifier (must match a key in `CLIENT_ROUTES`) |

**Responses:**

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status":"accepted","request_id":"voice-0001","transcript":"..."}` | STT succeeded, background processing started |
| `400` | `{"error":"Bad request","detail":"..."}` | Missing source, unknown source, missing audio, empty file |
| `401` | `{"error":"Unauthorized","detail":"..."}` | Missing or invalid Bearer token |
| `500` | `{"error":"config","detail":"..."}` | Server misconfigured (missing env var) |
| `502` | `{"error":"STT","detail":"..."}` | Groq transcription failed |

**Example:**
```bash
curl -X POST http://<mac-mini-tailscale-ip>:18790/voice \
  -H "Authorization: Bearer <token>" \
  -F "audio=@recording.wav" \
  -F "source=mbp-dev"
```

#### `GET /health`

Health check endpoint. No authentication required.

**Auth:** None

**Response:**

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status":"ok","version":"2.1.0-async","tts_provider":"grok","routes_configured":2}` | Bridge running, N client routes configured |

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `status` | string | Always `"ok"` when the server is up |
| `version` | string | Bridge software version |
| `tts_provider` | string | Active TTS backend (`"grok"` or `"elevenlabs"`) |
| `routes_configured` | integer | Number of entries in `CLIENT_ROUTES` — should match the number of active listening posts |

**Example:**
```bash
curl http://localhost:18790/health
# → {"routes_configured":2,"status":"ok","tts_provider":"grok","version":"2.1.0-async"}

# From another host (use Mac mini LAN or Tailscale IP):
curl http://<mac-mini-lan-ip>:18790/health
curl http://<mac-mini-tailscale-ip>:18790/health
```

**Use cases:**
- Verify Bridge is running after restart
- Confirm `CLIENT_ROUTES` update took effect (`routes_configured` count)
- Check which TTS provider is active

---

### Playback Server Endpoints (MBP :18780)

#### `POST /play`

Receive and play WAV audio. Returns as soon as `afplay` is spawned.

**Auth:** `Authorization: Bearer <PLAYBACK_BEARER_TOKEN>`

**Accepted request formats:**

| Content-Type | Body |
|---|---|
| `audio/wav` | Raw WAV bytes |
| `multipart/form-data` | `audio` field containing WAV file |

**Responses:**

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"status":"playing"}` | WAV received, `afplay` spawned |
| `400` | `{"error":"missing audio field"}` | Multipart request without `audio` field |
| `400` | `{"error":"empty body"}` | Empty request body |
| `401` | (no body) | Missing or invalid Bearer token |

**Example (raw WAV):**
```bash
curl -X POST http://<mbp-lan-ip>:18780/play \
  -H "Authorization: Bearer <PLAYBACK_BEARER_TOKEN>" \
  -H "Content-Type: audio/wav" \
  --data-binary @reply.wav
```

**Example (multipart):**
```bash
curl -X POST http://<mbp-lan-ip>:18780/play \
  -H "Authorization: Bearer <PLAYBACK_BEARER_TOKEN>" \
  -F "audio=@reply.wav"
```

#### `GET /health`

Health check. No authentication required.

**Response:** `200 {"status":"ok","version":"1.0.0","port":18780}`

```bash
curl http://localhost:18780/health
```

---

## 11. External Services

### Groq (Speech-to-Text)

- **API:** `https://api.groq.com/openai/v1/audio/transcriptions`
- **Model:** `whisper-large-v3`
- **Account:** Created 2026-04-12
- **Pricing:** Free tier available; check current limits at groq.com
- **Typical latency:** 300-500ms
- **Input:** WAV audio (multipart upload)
- **Output:** JSON with `text` field containing transcript

### ElevenLabs (Text-to-Speech)

- **API:** `https://api.elevenlabs.io/v1/text-to-speech/<voice_id>`
- **Model:** `eleven_v3`
- **Current voice ID:** `<elevenlabs-voice-id>` (configurable via `ELEVENLABS_VOICE_ID`)
- **Default fallback voice ID (hardcoded):** `EXAVITQu4vr4xnSDxMaL` (Sarah)
- **Output format:** `pcm_16000` (raw PCM, wrapped into WAV by the bridge)
- **Typical latency:** 800ms-4s depending on text length
- **Future consideration:** Grok TTS as cheaper alternative (TBD)

### OpenClaw Chat Completions API

- **Endpoint:** `http://127.0.0.1:18789/v1/chat/completions` (localhost only)
- **Auth:** Gateway HTTP token (different from hooks token)
- **Enabled via:** `gateway.http.endpoints.chatCompletions.enabled: true` in `openclaw.json`
- **Model routing:** `openclaw/ceo` → Jarvis CEO agent (full delegation power)
- **Session persistence:** The `user` field (`voice-client`) gives Voice its own stable session across turns
- **Behavior:** Synchronous — blocks until the full agent turn completes (including any sub-agent delegation)

---

## 12. Testing

### Health Check
```bash
curl http://localhost:18790/health
# → {"status":"ok","version":"2.0.0-async","routes_configured":1}
```

### Smoke Test: STT + 202 Response
```bash
curl -s -w "\nHTTP: %{http_code}\n" \
  -X POST http://localhost:18790/voice \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  -F "audio=@test.wav" \
  -F "source=mbp-dev"
# → {"request_id":"voice-0001","status":"accepted","transcript":"..."}\nHTTP: 202
```

### Smoke Test: Playback Server Health (MBP)
```bash
curl http://localhost:18780/health
# → {"port":18780,"status":"ok","version":"1.0.0"}
```

### Smoke Test: Playback POST (local, on MBP)
```bash
# Use any WAV or AIFF file — afplay accepts both
curl -s -w "\nHTTP: %{http_code}\n" \
  -X POST http://localhost:18780/play \
  -H "Authorization: Bearer <PLAYBACK_BEARER_TOKEN>" \
  -H "Content-Type: audio/wav" \
  --data-binary @/System/Library/Sounds/Ping.aiff
# → {"status":"playing"}\nHTTP: 200  (Ping sound plays)
```

### Smoke Test: Playback POST (from Mac mini to MBP, cross-device)
```bash
# Run on Mac mini
curl -s -w "\nHTTP: %{http_code}\n" \
  -X POST http://<mbp-tailscale-ip>:18780/play \
  -H "Authorization: Bearer <CLIENT_PLAYBACK_TOKEN>" \
  -H "Content-Type: audio/wav" \
  --data-binary @reply.wav
# → {"status":"playing"}\nHTTP: 200
```

### Smoke Test: Auth rejection
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:18780/play \
  -H "Authorization: Bearer wrong-token" \
  -H "Content-Type: audio/wav" \
  --data-binary @reply.wav
# → 401
```

### Smoke Test: Voice Client upload only (no wake word required)
```bash
# Record a quick test WAV first, then:
curl -s -w "\nHTTP: %{http_code}\n" \
  -X POST http://<mac-mini-lan-ip>:18790/voice \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  -F "audio=@test.wav" \
  -F "source=mbp-dev"
# → {"request_id":"voice-0001","status":"accepted","transcript":"..."}\nHTTP: 202
# Then watch MBP speakers — reply should arrive ~6-12s later
```

### Full End-to-End Test
1. Ensure Bridge is running on Mac mini: `launchctl list | grep voice-bridge`
2. Load and verify Playback Server on MBP:
   ```bash
   mkdir -p /tmp/jarvis
   launchctl load ~/Library/LaunchAgents/com.jarvis.playback-server.plist
   launchctl list | grep playback
   curl http://localhost:18780/health
   ```
3. Start Voice Client on MBP:
   ```bash
   cd <project-root>/Projects/openWakeWorld/openWakeWord
   python3 jarvis_voice_client.py
   ```
4. Say "Hey Jarvis, wie spät ist es?"
5. Expected sequence:
   - Terminal shows `[WAKE DETECTED]`, `[RECORDING]`, `[UPLOADED] 202 Accepted`, `[LISTENING]`
   - ~6-12s later: MBP speakers play Jarvis's reply
   - Bridge log shows `[COMPLETE]` with full timing
6. Check Bridge logs: `tail -20 /tmp/openclaw/voice-bridge-stderr.log`
7. Check Playback Server logs: `tail -10 /tmp/jarvis/playback_server.log`

### Pi Smoke Tests

```bash
# From MBP: Pi playback server reachable?
curl http://<pi-lan-ip>:18780/health
# → {"port":18780,"status":"ok","version":"1.0.0"}

# Pi voice-client listening?
ssh <pi-hostname> "tail -5 <project-root>/jarvis-voice/logs/voice-client.log"
# → last line should be [LISTENING] Waiting for 'Hey Jarvis'…

# Test playback (from MBP, sound plays on Pi speaker):
curl -X POST http://<pi-lan-ip>:18780/play \
  -H "Authorization: Bearer <PLAYBACK_BEARER_TOKEN>" \
  -H "Content-Type: audio/wav" \
  --data-binary @/path/to/any.wav
# → {"status":"playing"}

# From Mac mini: verify Pi playback reachable (Task G):
curl -v http://<pi-lan-ip>:18780/health
```

### Multi-client Routing Verification

After Bridge `CLIENT_ROUTES` has been updated to include `pi-livingroom`:

```bash
# Bridge health should show routes_configured=2:
curl http://<mac-mini-lan-ip>:18790/health
# → {"routes_configured":2,"status":"ok","version":"2.0.0-async"}

# Upload directly as pi-livingroom (sound plays on Pi):
curl -X POST http://<mac-mini-lan-ip>:18790/voice \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  -F "audio=@test.wav" \
  -F "source=pi-livingroom"
# → 202 Accepted — reply plays on Pi speaker

# Upload as mbp-dev (sound plays on MBP):
curl -X POST http://<mac-mini-lan-ip>:18790/voice \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  -F "audio=@test.wav" \
  -F "source=mbp-dev"
# → 202 Accepted — reply plays on MBP speaker
```

### Validation Checklist (from project spec)
- [ ] `POST /voice` returns 202 in under 2s
- [ ] Background task completes and calls client `/play` successfully
- [ ] Repeat 5 times without restart — neither service crashes
- [ ] Errors (bad audio, unreachable client) are caught and logged, not crashed

---

## 13. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Bridge not running | `launchctl list \| grep voice-bridge` | `launchctl load ~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist` |
| 401 on /voice | Bearer token mismatch | Compare `BRIDGE_TOKEN` in Bridge .env with client .env |
| 400 "Unknown source" | Source not in CLIENT_ROUTES | Add source to `CLIENT_ROUTES` JSON in .env, restart |
| 502 on STT | Groq API issue | Check `GROQ_API_KEY`, check Groq status page, check logs for details |
| [AGENT] FAILED | OpenClaw down or timeout | Check OpenClaw Gateway: `curl http://localhost:18789/health`. Check `OPENCLAW_API_TOKEN`. Increase `AGENT_TIMEOUT_SECONDS` |
| [TTS] FAILED | ElevenLabs issue | Check `ELEVENLABS_API_KEY`, check quota at elevenlabs.io |
| [PLAYBACK-POST] FAILED "Connection refused" | Playback server not running on MBP | `launchctl load ~/Library/LaunchAgents/com.jarvis.playback-server.plist` |
| [PLAYBACK-POST] FAILED timeout | Network/Tailscale issue | `tailscale status`. Try LAN IP (`<mbp-lan-ip>`) instead of Tailscale IP |
| No sound on MBP | Audio routing or wrong default device | Check MBP volume; test manually: `afplay /System/Library/Sounds/Ping.aiff` |
| Playback server 401 | Token mismatch | Compare `PLAYBACK_BEARER_TOKEN` in MBP `.env` with `CLIENT_PLAYBACK_TOKEN` in Bridge `.env` |
| Playback server not starting | venv or Flask missing | `ls <project-root>/Projects/openWakeWorld/openWakeWord/venv/bin/flask` — if missing: `venv/bin/pip install flask python-dotenv` |
| Playback server starts but unreachable from Bridge | Binding to localhost only | Confirm `app.run(host="0.0.0.0", ...)` in `playback_server.py`. Check firewall: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --listapps` |
| Voice client: `[ERROR] upload timed out after 10s` | Bridge slow or unreachable | Check Bridge health: `curl http://<mac-mini-lan-ip>:18790/health`. If Bridge is busy with a long agent turn, wait — the 10s timeout is for the 202-ack, not agent processing |
| Voice client: `[ERROR] network error` | Bridge offline or wrong URL | Check `OPENCLAW_WEBHOOK_URL` in `.env`. Ping Bridge: `curl http://<mac-mini-lan-ip>:18790/health` |
| Wake word too sensitive (false triggers) | Threshold too low | Increase `WAKE_THRESHOLD` in `jarvis_voice_client.py` (e.g. `0.6` or `0.7`) |
| Wake word not triggering | Threshold too high or mic issue | Lower `WAKE_THRESHOLD`. Check mic with `python3 -c "import pyaudio; p=pyaudio.PyAudio(); print(p.get_default_input_device_info())"` |
| Recording cuts off too early | Silence RMS too high | Lower `SILENCE_RMS_THRESH` (e.g. `200`). Room may be quieter than calibration environment |
| Recording never stops | Silence RMS too low or background noise | Raise `SILENCE_RMS_THRESH`. Check ambient noise level |
| Agent replies "No response from OpenClaw" | Phantom wake-word trigger (false positive) sending near-empty audio | Check Bridge log: if transcript is `"."` or `"You"` with ~64KB audio, it's a false trigger from post-recording noise. Fix: add cooldown after playback, raise wake threshold, or filter minimum audio length |
| Bridge logs not appearing | LaunchAgent stderr path | `mkdir -p /tmp/openclaw` |
| Playback server logs not appearing | `/tmp/jarvis` does not exist | `mkdir -p /tmp/jarvis`, then restart LaunchAgent |
| **Pi: voice-client log empty** | Python output buffering | Confirm `PYTHONUNBUFFERED=1` in `~/.config/systemd/user/jarvis-voice-client.service` → `systemctl --user daemon-reload && restart` |
| **Pi: `[LISTENING]` never appears after 2+ min** | Model load stuck or crash | Check `voice-client.err` for Python traceback (filter ALSA noise first). Try manual run: `cd ~/jarvis-voice && source venv/bin/activate && python3 jarvis_voice_client.py` |
| **Pi: `OSError: Invalid sample rate`** | PyAudio opened with `rate=16000` directly on hw:2,0 | Must open at 48000Hz/stereo and downsample in Python. Check that the correct `jarvis_voice_client.py` (Pi version with `NATIVE_SAMPLE_RATE=48000`) is deployed |
| **Pi: `OSError: Invalid number of channels`** | PyAudio device index wrong | Stop service, re-enumerate: `venv/bin/python3 -c "import pyaudio; p=pyaudio.PyAudio(); [print(i,p.get_device_info_by_index(i)['name'],p.get_device_info_by_index(i)['maxInputChannels']) for i in range(p.get_device_count())]"`. Update `INPUT_DEVICE_INDEX` in `.env` |
| **Pi: USB mic not found by PyAudio** | Service running = device locked | The voice-client service holds `hw:2,0`. Stop service before enumerating: `systemctl --user stop jarvis-voice-client.service` |
| **Pi: `~/.asoundrc` breaks mic enumeration** | `type asym` confuses PortAudio | Remove `~/.asoundrc` entirely. Use Python-side downsampling instead |
| **Pi: no sound from speaker** | Wrong ALSA device | Test: `aplay -D plughw:3,0 /tmp/jarvis/playback_*.wav`. Check `ALSA_PLAYBACK_DEVICE` in `.env` matches `aplay -l` output |
| **Pi: reply not routed to Pi** | `CLIENT_ROUTES` missing `pi-livingroom` | Bridge `.env` on Mac mini: `CLIENT_ROUTES` must include `"pi-livingroom":"http://<pi-lan-ip>:18780/play"`. Restart bridge after edit |
| **Pi: no sound after reboot / mic not found after reboot** | USB card number drift | ALSA assigns card numbers by USB plug order, not persistently. After a reboot or reconnect, card 2 and 3 may shift. Check: `ssh <pi-hostname> "cat /proc/asound/cards"`. If speaker is no longer card 3 or mic no longer card 2, update `ALSA_PLAYBACK_DEVICE` and `INPUT_DEVICE_INDEX` in `<project-root>/jarvis-voice/.env`, then `<pi-hostname>-restart-all` |
| **Pi: service not starting after reboot** | Linger not set | `sudo loginctl enable-linger jarvis` |

---

## 14. File Inventory

### Mac mini (`<project-root>/projects/Jarvis-remote-endpoint/`)

| File | Purpose |
|---|---|
| `voice_bridge.py` | Main bridge service (v2 async) |
| `.env` | All configuration and secrets |
| `requirements.txt` | Python dependencies |
| `.venv/` | Python virtual environment |
| `jarvis-voice-project_v2.md` | Project spec / architecture guide |
| `DOCUMENTATION.md` | This file |
| `test_voice.sh` | Legacy test script (Phase A) |
| `reply.wav` | Test WAV file for smoke tests |
| `gemini_tts_neutral.wav` | Test WAV from Gemini TTS experiment |
| `jarvis-voice-loop-phase-a.md` | Phase A spec (historical) |
| `PROJECT.md` | Legacy project notes |
| `README.md` | Legacy readme |

### Mac mini (LaunchAgent)

| File | Purpose |
|---|---|
| `~/Library/LaunchAgents/ai.openclaw.voice-bridge.plist` | LaunchAgent for auto-start + restart |

### Mac mini (Logs)

| File | Purpose |
|---|---|
| `/tmp/openclaw/voice-bridge-stdout.log` | Stdout (usually empty — Waitress logs to stderr) |
| `/tmp/openclaw/voice-bridge-stderr.log` | All application logs |

### MBP (`<project-root>/Projects/openWakeWorld/openWakeWord/`)

| File | Purpose |
|---|---|
| `jarvis_voice_client.py` | Wake word detection, recording, async upload to Bridge |
| `playback_server.py` | Flask server: receives WAV from Bridge, plays via `afplay` |
| `.env` | All config for both client scripts (bridge URL, tokens, ports) |
| `venv/` | Python 3.9 virtual environment |
| `venv/bin/python3` | Python interpreter used by LaunchAgent |

### MBP (LaunchAgent)

| File | Purpose |
|---|---|
| `~/Library/LaunchAgents/com.jarvis.playback-server.plist` | Auto-start + auto-restart for playback server |

### MBP (Logs & Temp Files)

| Path | Purpose |
|---|---|
| `/tmp/jarvis/playback_server.log` | Playback server stdout |
| `/tmp/jarvis/playback_server.err` | Playback server stderr |
| `/tmp/jarvis/playback_<timestamp>.wav` | Received reply audio (accumulates, clean manually) |
| `/tmp/jarvis/jarvis_turn_<timestamp>.wav` | Recorded user speech (accumulates, clean manually) |

### Raspberry Pi (`<project-root>/jarvis-voice/`)

| File | Purpose |
|---|---|
| `jarvis_voice_client.py` | Wake word + recording + upload (Pi version: 48kHz/stereo + downsampling) |
| `playback_server.py` | Flask server: receives WAV from Bridge, plays via `aplay` |
| `.env` | Config: bridge URL, tokens, INPUT_DEVICE_INDEX, ALSA_PLAYBACK_DEVICE |
| `venv/` | Python 3.13 virtual environment |

### Pi (systemd Services)

| File | Purpose |
|---|---|
| `~/.config/systemd/user/jarvis-playback.service` | Auto-start + auto-restart for playback server |
| `~/.config/systemd/user/jarvis-voice-client.service` | Auto-start + auto-restart for voice client |

### Pi (Logs & Temp Files)

| Path | Purpose |
|---|---|
| `~/jarvis-voice/logs/voice-client.log` | Voice client stdout |
| `~/jarvis-voice/logs/voice-client.err` | Voice client stderr (ALSA noise + real errors) |
| `~/jarvis-voice/logs/playback.log` | Playback server stdout |
| `~/jarvis-voice/logs/playback.err` | Playback server stderr |
| `/tmp/jarvis/playback_<timestamp>.wav` | Received reply audio (accumulates, clean manually) |
| `/tmp/jarvis/jarvis_turn_<timestamp>.wav` | Recorded user speech (accumulates, clean manually) |

### Project Root (`<project-root>/Projects/openWakeWorld/`)

| File | Purpose |
|---|---|
| `jarvis-voice-project_v2.md` | Phase A+B MBP spec (historical reference) |
| `jarvis-voice-project_v3-pi.md` | Phase B Pi portierung spec + implementation notes |
| `DOCUMENTATION.md` | This file |

---

## 15. Known Limitations & Future Work

### Current Limitations

- **No retries.** If the playback POST fails (client offline), the reply is dropped.
- **No TLS.** All communication is plaintext HTTP. Acceptable on Tailscale/LAN, not for public internet.
- **No interrupt / barge-in.** If you speak while Jarvis is replying, nothing happens.
- **No Talk Mode.** Each interaction requires the wake word. Continuous conversation is not supported.
- **Single voice session.** The `voice-client` user string is shared across all voice clients. Multiple simultaneous speakers would share context.
- **Request counter resets on restart.** Not a real issue (log timestamps provide ordering).
- **No audio format validation.** The bridge trusts that uploaded files are valid WAV. Corrupt files will fail at Groq.

### Completed in Phase B (2026-04-15)

- **Raspberry Pi deployment** — ✅ voice_client.py + playback_server.py laufen als systemd services
- **Multi-client routing** — ✅ `CLIENT_ROUTES`: MBP via Tailscale (`<mbp-tailscale-ip>`), Pi via LAN (`<pi-lan-ip>`), `routes_configured=2` verifiziert
- **Gateway token rotation** — ✅ `OPENCLAW_API_TOKEN` nach Security Audit (2026-04-15) aktualisiert. Symptom war HTTP 401 beim Agent-Call. Fix: Token aus `gateway.auth.token` in `openclaw.json` übernehmen, Bridge neu laden. Relevant für zukünftige Token-Rotationen.
- **Pi reachability from Mac mini** — ✅ `curl http://<pi-lan-ip>:18780/health` → 200 OK (312ms cross-subnet, 2ms via mDNS-Hostname)
- **First successful Pi voice loop** — ✅ 2026-04-15 14:25: "Wie wird das Wetter morgen?" → STT 464ms → Agent 14.4s → TTS 6.9s → Pi-Speaker (total 21.8s)
- **Phantom-trigger fix** — ✅ `POST_UPLOAD_COOLDOWN=5.0s` + `MIN_RECORD_SECONDS=1.5s` in beiden Scripts (MBP + Pi). Verifiziert: `[COOLDOWN] wake ignored (score=0.991) — 4.5s remaining`

### Planned (Phase C+)

- **Grok TTS as cheap alternative** — switchable via .env
- **Custom "Jarvis" voice** — ElevenLabs voice library experiment
- **Talk Mode** — continuous conversation without wake word (post-reply listening window)
- **Interrupt detection** — stop playback if user starts speaking
- **Retry logic** — for failed playback POSTs (with backoff)
- **Mic auto-detection** — dynamically find correct PyAudio index on startup (Pi: index can shift after USB reconnect)
- **Per-source tokens** — currently all clients share the same `CLIENT_PLAYBACK_TOKEN`
- **Multiple Pi listening posts** — architecturally trivial, just add more `CLIENT_ROUTES` entries

---

---

## 16. MBP Aliases for Pi Maintenance

Add to `~/.zshrc` on the MBP, then `source ~/.zshrc`.

```bash
# ── Jarvis Pi: Logs ────────────────────────────────────────────────────────
# Voice-Client live: Wake, Recording, Upload, Cooldown
alias <pi-hostname>-log="ssh <pi-hostname> 'tail -n 100 -f <project-root>/jarvis-voice/logs/voice-client.log'"

# Playback-Server live: wann kommt die Antwort an
alias <pi-hostname>-play-log="ssh <pi-hostname> 'tail -n 50 -f <project-root>/jarvis-voice/logs/playback.log'"

# Beide gleichzeitig (==> Dateiname <== als Trennlinie)
alias <pi-hostname>-logs="ssh <pi-hostname> 'tail -n 50 -f <project-root>/jarvis-voice/logs/voice-client.log <project-root>/jarvis-voice/logs/playback.log'"

# Nur echte Fehler — ALSA-Rauschen rausgefiltert
alias <pi-hostname>-errors="ssh <pi-hostname> \"grep -v 'ALSA\|jack\|Cannot connect\|JackShm\|DiscoverDevice\|GPU device\|drm\|sys/class' <project-root>/jarvis-voice/logs/voice-client.err | tail -30\""

# ── Jarvis Pi: Status & Kontrolle ─────────────────────────────────────────
# Status beider Services
alias <pi-hostname>-status="ssh <pi-hostname> 'systemctl --user status jarvis-playback.service jarvis-voice-client.service --no-pager'"

# Health-Check vom MBP aus
alias <pi-hostname>-health="curl -s http://<pi-lan-ip>:18780/health | python3 -m json.tool"

# Voice-Client neu starten (nach Code- oder .env-Änderung)
alias <pi-hostname>-restart="ssh <pi-hostname> 'systemctl --user restart jarvis-voice-client.service && echo restarted'"

# Beide Services neu starten
alias <pi-hostname>-restart-all="ssh <pi-hostname> 'systemctl --user restart jarvis-playback.service jarvis-voice-client.service && echo both restarted'"

# ── Jarvis Pi: Audio ───────────────────────────────────────────────────────
# Lautstärke interaktiv (Pfeiltasten hoch/runter, ESC beenden)
alias <pi-hostname>-vol="ssh -t <pi-hostname> 'alsamixer -c 3'"

# Lautstärke direkt setzen: <pi-hostname>-setvol 70%
# Als Funktion — alias kann $1 nicht durch Single-Quotes durchreichen
<pi-hostname>-setvol() { ssh <pi-hostname> "amixer -c 3 sset Speaker $1"; }

# ── Jarvis Pi: Housekeeping ────────────────────────────────────────────────
# Temp-WAVs aufräumen (akkumulieren in /tmp/jarvis/)
alias <pi-hostname>-clean="ssh <pi-hostname> 'rm -f /tmp/jarvis/*.wav && echo cleaned'"

# Logs leeren (werden endlos angehängt — gelegentlich rotieren)
alias <pi-hostname>-cleanlogs="ssh <pi-hostname> '> <project-root>/jarvis-voice/logs/voice-client.log; > <project-root>/jarvis-voice/logs/voice-client.err; > <project-root>/jarvis-voice/logs/playback.log; > <project-root>/jarvis-voice/logs/playback.err; echo logs cleared'"
```

**Quick Reference:**

| Alias | Wann benutzen |
|---|---|
| `<pi-hostname>-log` | Live mitlesen ob Wake-Word erkannt wird |
| `<pi-hostname>-logs` | Wake + Playback gleichzeitig beobachten |
| `<pi-hostname>-errors` | Nach einem Bug — echte Fehler ohne ALSA-Rauschen |
| `<pi-hostname>-status` | Nach Reboot prüfen ob beide Services laufen |
| `<pi-hostname>-health` | Schnell-Check ob Playback-Server erreichbar |
| `<pi-hostname>-restart` | Nach Änderung an `voice_client.py` oder `.env` |
| `<pi-hostname>-restart-all` | Nach Änderung an `playback_server.py` |
| `<pi-hostname>-vol` | Lautstärke zu laut oder zu leise |
| `<pi-hostname>-setvol 60%` | Lautstärke direkt auf Wert setzen |
| `<pi-hostname>-clean` | `/tmp/jarvis/` wächst (WAVs akkumulieren) |
| `<pi-hostname>-cleanlogs` | Logs sind zu groß geworden |

---

*Documentation maintained jointly by Jarvis (Mac mini / Bridge side) and Claude Code (MBP + Pi / Client + Playback side). Last full review: 2026-04-15.*
