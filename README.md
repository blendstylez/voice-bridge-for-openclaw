# voice-bridge-for-openclaw

An async voice interface for [OpenClaw](https://openclaw.ai). Talk to your homelab AI agent like you'd talk to a smart assistant — wake word, speech recognition, reply, done. Multi-client, LAN-native, built to not block the agent.

```
  "Hey Jarvis, turn off the               "Done. Bathroom light is off."
   light in the bathroom."                         ▲
        │                                          │
        ▼                                          │
┌────────────────────┐                  ┌────────────────────┐
│   MacBook / Pi     │                  │   MacBook / Pi     │
│   voice_client     │                  │   playback_server  │
│   (wake + record)  │                  │   (afplay / aplay) │
└─────────┬──────────┘                  └─────────▲──────────┘
          │ POST /voice                           │ POST /play
          │ (audio + source)                      │ (wav)
          ▼                                       │
┌─────────────────────────────────────────────────┴──────────────┐
│                  Mac mini (Bridge, :18790)                      │
│                                                                 │
│  Groq Whisper  →  OpenClaw agent  →  ElevenLabs TTS             │
│       ~400ms        2s – 2min           ~1s                     │
│                                                                 │
│  Returns 202 immediately, pushes audio back when reply is ready │
└─────────────────────────────────────────────────────────────────┘
```

## What you can do with it

Whatever your agent can do. A few real queries from daily use:

- *"Hey Jarvis, turn off the light in the bathroom."* (Home Assistant)
- *"Hey Jarvis, is any door or window open?"* (Home Assistant state check)
- *"Hey Jarvis, did the nightly backup cronjob finish successfully?"* (OpenClaw task)
- *"Hey Jarvis, how long to the office right now?"* (traffic / calendar mashup)

The voice layer doesn't care what the agent does — it just routes audio in, text through, audio back. If your agent can do it with text, it can do it with voice.

## Why this exists

Most voice-to-LLM setups fall into one of two traps:

1. **They block.** The client waits on a 10-second HTTP call for the agent to reply, times out, and the whole thing feels broken.
2. **They assume one device.** Fine if you only ever talk from one place. Useless if you want your Mac to listen in the office and a Pi in the living room.

This project solves both:

- **Fully async.** Client uploads audio, gets `202 Accepted` in under a second, goes back to listening. Bridge processes in a background thread and pushes the reply when ready. No timeouts. Agents can take their time.
- **Multi-client routing.** Each client sends a `source` identifier (`mbp-dev`, `pi-livingroom`, whatever). The Bridge looks up where the reply should go and POSTs it there. No cross-talk.

## Features

- Wake word detection via [openWakeWord](https://github.com/dscripka/openWakeWord) (`hey_jarvis` by default — yes, the Iron Man one)
- STT via Groq Whisper (~350ms typical)
- LLM agent turn via OpenClaw Chat Completions API
- TTS via ElevenLabs (or swappable — the abstraction is one function)
- Non-blocking playback on client side (`afplay` on macOS, `aplay` on Raspberry Pi OS)
- Bearer-token auth on both directions
- Health endpoints on every service for monitoring
- Works over LAN and Tailscale
- Process management via LaunchAgent (macOS) and systemd user services (Pi)

## Project Structure

```
voice-bridge-for-openclaw/
├── bridge/                       Mac mini side
│   ├── voice_bridge.py           Main bridge service (async, ~500 lines)
│   ├── requirements.txt
│   ├── .env.example              Config template
│   └── launchagent.plist.example macOS auto-start template
│
├── clients/
│   ├── mbp/                      MacBook Pro client
│   │   ├── voice_client.py       Wake word + record + upload
│   │   ├── playback_server.py    Flask server: receives WAV, plays with afplay
│   │   ├── requirements.txt
│   │   ├── .env.example
│   │   ├── launchagent.plist.example
│   │   └── aliases.zsh.example   Handy aliases for managing the Pi from here
│   │
│   └── pi/                       Raspberry Pi client
│       ├── voice_client.py       Same idea, with 48kHz→16kHz downsampling
│       ├── playback_server.py    Uses aplay via ALSA
│       ├── requirements.txt
│       ├── .env.example
│       ├── jarvis-voice-client.service.example
│       └── jarvis-playback.service.example
│
└── docs/
    └── DOCUMENTATION.md          Full technical reference (everything)
```

## Quick Start

The full setup guide lives in [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md) — it's long, because it covers Mac mini bridge setup, MBP client, Raspberry Pi client (including the fun ALSA `asoundrc` trap), process management, troubleshooting, and the HTTP API reference.

The short version:

1. **Pick your setup.** You need at minimum: a Mac running OpenClaw (the bridge host) + one client device (Mac or Pi).
2. **Set up the bridge** on the Mac mini — `bridge/voice_bridge.py`, fill in `bridge/.env.example` → `.env` with your Groq / OpenClaw / ElevenLabs tokens, start it via the provided LaunchAgent.
3. **Set up a client** — copy `clients/mbp/` or `clients/pi/` contents, fill in `.env`, configure process management (LaunchAgent or systemd user service).
4. **Add the client's playback URL** to the bridge's `CLIENT_ROUTES` env var.
5. **Say "Hey Jarvis."**

## Status

Working. Runs daily on the maintainer's homelab. Multi-client mode (MBP + Raspberry Pi) has been in production since 2026-04-15.

**Known limits** (see `DOCUMENTATION.md` section 15):

- No TLS. Plaintext HTTP on LAN/Tailscale is the design point.
- No interrupt / barge-in — can't interrupt the bot while it's speaking.
- No continuous conversation mode — wake word required every turn.
- No retries on failed playback POSTs.

None of these are hard to add; they're just not priorities for a personal setup.

## Background

This is the voice layer for a homelab AI agent built on [OpenClaw](https://openclaw.ai). Designed for self-hosters who want a real assistant (with delegation, tool use, reasoning time) rather than a single-turn LLM chatbot with a microphone slapped on.

Not a product. Not a startup. Just an open mind with a lot of ideas.

## License

MIT. See [`LICENSE`](LICENSE).

---

*Built by a human + two AI collaborators: OpenClaw's Jarvis (bridge) and Claude Code (clients).*
