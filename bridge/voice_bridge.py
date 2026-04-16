#!/usr/bin/env python3
"""OpenClaw Voice Bridge, Async Architecture (v2.1).

Receives audio from voice clients, transcribes via Groq Whisper,
hands the transcript to Jarvis (OpenClaw Chat Completions API),
synthesizes the reply via Grok TTS or ElevenLabs TTS, and POSTs
the resulting WAV back to the client's playback server.

The HTTP handler returns 202 Accepted immediately after STT.
All downstream work (agent turn, TTS, playback POST) runs in a
background thread so the client can return to listening instantly.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "18790"))
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_TRANSCRIPTION_MODEL = "whisper-large-v3"

OPENCLAW_API_URL = os.getenv("OPENCLAW_API_URL", "http://127.0.0.1:18789/v1/chat/completions")
OPENCLAW_API_TOKEN = os.getenv("OPENCLAW_API_TOKEN", "")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "openclaw/ceo")
OPENCLAW_USER = os.getenv("OPENCLAW_USER", "voice-client")

# TTS provider selection: "grok" (default) or "elevenlabs"
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "grok").lower()

# Grok TTS (xAI)
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
GROK_TTS_URL = "https://api.x.ai/v1/tts"
GROK_TTS_VOICE = os.getenv("GROK_TTS_VOICE", "sal")
GROK_TTS_LANGUAGE = os.getenv("GROK_TTS_LANGUAGE", "de")

# ElevenLabs TTS (fallback)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVENLABS_MODEL_ID = "eleven_v3"
ELEVENLABS_OUTPUT_FORMAT = "pcm_16000"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"

# Agent turn timeout — configurable via .env
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))
STT_TIMEOUT_SECONDS = 60
TTS_TIMEOUT_SECONDS = 60

# Client routing table: JSON mapping source names to playback URLs.
# Example: {"mbp-dev":"http://<client-host-or-ip>:18780/play"}
CLIENT_ROUTES_RAW = os.getenv("CLIENT_ROUTES", "{}")
try:
    CLIENT_ROUTES: dict[str, str] = json.loads(CLIENT_ROUTES_RAW)
except json.JSONDecodeError:
    CLIENT_ROUTES = {}

# Shared bearer token for POSTing to client playback servers.
CLIENT_PLAYBACK_TOKEN = os.getenv("CLIENT_PLAYBACK_TOKEN", "")

# Audio format constants (16 kHz mono 16-bit PCM).
PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2

LOG_TEXT_PREVIEW_LENGTH = 120

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("voice_bridge")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class BridgeError(Exception):
    stage: str
    detail: str


def require_env(name: str, value: str) -> None:
    if not value:
        raise BridgeError("config", f"Missing required env var: {name}")


def preview(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= LOG_TEXT_PREVIEW_LENGTH:
        return cleaned
    return cleaned[:LOG_TEXT_PREVIEW_LENGTH] + "..."


def pcm_to_wav_bytes(pcm_audio: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wf.setframerate(PCM_SAMPLE_RATE)
        wf.writeframes(pcm_audio)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def transcribe_audio(filename: str, audio_bytes: bytes) -> str:
    """[STT] Groq Whisper transcription."""
    require_env("GROQ_API_KEY", GROQ_API_KEY)

    url = f"{GROQ_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, audio_bytes, "audio/wav")}
    data = {"model": GROQ_TRANSCRIPTION_MODEL}

    try:
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=STT_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise BridgeError("STT", f"Groq request error: {exc}") from exc

    if not resp.ok:
        raise BridgeError("STT", f"Groq HTTP {resp.status_code}: {resp.text[:400]}")

    try:
        payload: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise BridgeError("STT", "Groq response not valid JSON") from exc

    transcript = str(payload.get("text", "")).strip()
    if not transcript:
        raise BridgeError("STT", "Groq returned empty transcript")

    return transcript


def ask_jarvis(transcript: str) -> str:
    """[AGENT] Send transcript to OpenClaw Chat Completions API (blocking)."""
    require_env("OPENCLAW_API_TOKEN", OPENCLAW_API_TOKEN)

    headers = {
        "Authorization": f"Bearer {OPENCLAW_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENCLAW_MODEL,
        "user": OPENCLAW_USER,
        "messages": [{"role": "user", "content": transcript}],
    }

    try:
        resp = requests.post(OPENCLAW_API_URL, headers=headers, json=payload, timeout=AGENT_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise BridgeError("AGENT", f"OpenClaw request error: {exc}") from exc

    if not resp.ok:
        raise BridgeError("AGENT", f"OpenClaw HTTP {resp.status_code}: {resp.text[:400]}")

    try:
        result = resp.json()
    except ValueError:
        raise BridgeError("AGENT", "OpenClaw response not valid JSON")

    reply_text = _extract_chat_reply(result)
    if not reply_text:
        raise BridgeError("AGENT", "No reply text in OpenClaw response")

    return reply_text


def _extract_chat_reply(payload: dict) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts).strip()
    return ""


def synthesize_speech(text: str) -> bytes:
    """[TTS] Route to configured TTS provider → WAV bytes."""
    if TTS_PROVIDER == "grok":
        return _tts_grok(text)
    elif TTS_PROVIDER == "elevenlabs":
        return _tts_elevenlabs(text)
    else:
        raise BridgeError("TTS", f"Unknown TTS_PROVIDER: '{TTS_PROVIDER}'. Use 'grok' or 'elevenlabs'.")


def _tts_grok(text: str) -> bytes:
    """[TTS] Grok (xAI) text-to-speech → WAV bytes."""
    require_env("GROK_API_KEY", GROK_API_KEY)

    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "voice_id": GROK_TTS_VOICE,
        "language": GROK_TTS_LANGUAGE,
        "output_format": {
            "codec": "wav",
            "sample_rate": PCM_SAMPLE_RATE,
        },
    }

    try:
        resp = requests.post(GROK_TTS_URL, headers=headers, json=payload, timeout=TTS_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise BridgeError("TTS", f"Grok TTS request error: {exc}") from exc

    if not resp.ok:
        raise BridgeError("TTS", f"Grok TTS HTTP {resp.status_code}: {resp.text[:400]}")

    wav_audio = resp.content
    if not wav_audio:
        raise BridgeError("TTS", "Grok TTS returned empty audio")

    return wav_audio


def _tts_elevenlabs(text: str) -> bytes:
    """[TTS] ElevenLabs text-to-speech → WAV bytes."""
    require_env("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY)

    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{ELEVENLABS_VOICE_ID}?output_format={ELEVENLABS_OUTPUT_FORMAT}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "application/octet-stream",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=TTS_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise BridgeError("TTS", f"ElevenLabs request error: {exc}") from exc

    if not resp.ok:
        raise BridgeError("TTS", f"ElevenLabs HTTP {resp.status_code}: {resp.text[:400]}")

    pcm_audio = resp.content
    if not pcm_audio:
        raise BridgeError("TTS", "ElevenLabs returned empty audio")

    return pcm_to_wav_bytes(pcm_audio)


def post_to_client(source: str, wav_bytes: bytes) -> None:
    """[PLAYBACK-POST] Push WAV to the client's playback server."""
    playback_url = CLIENT_ROUTES.get(source)
    if not playback_url:
        raise BridgeError("PLAYBACK-POST", f"No route configured for source '{source}'")

    headers: dict[str, str] = {"Content-Type": "audio/wav"}
    if CLIENT_PLAYBACK_TOKEN:
        headers["Authorization"] = f"Bearer {CLIENT_PLAYBACK_TOKEN}"

    try:
        resp = requests.post(playback_url, headers=headers, data=wav_bytes, timeout=30)
    except requests.RequestException as exc:
        raise BridgeError("PLAYBACK-POST", f"Client unreachable at {playback_url}: {exc}") from exc

    if not resp.ok:
        raise BridgeError("PLAYBACK-POST", f"Client returned HTTP {resp.status_code}: {resp.text[:400]}")


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _background_reply(source: str, transcript: str, request_id: str) -> None:
    """Run the agent turn, TTS, and playback POST in a background thread."""
    try:
        # --- AGENT ---
        logger.info("[AGENT] Starting | req=%s | source=%s", request_id, source)
        agent_start = time.perf_counter()
        reply_text = ask_jarvis(transcript)
        agent_ms = round((time.perf_counter() - agent_start) * 1000)
        logger.info(
            '[AGENT] Done | req=%s | source=%s | %dms | reply="%s"',
            request_id, source, agent_ms, preview(reply_text),
        )

        # --- TTS ---
        logger.info("[TTS] Starting | req=%s | source=%s", request_id, source)
        tts_start = time.perf_counter()
        wav_bytes = synthesize_speech(reply_text)
        tts_ms = round((time.perf_counter() - tts_start) * 1000)
        logger.info(
            "[TTS] Done | req=%s | source=%s | %dms | wav_bytes=%d",
            request_id, source, tts_ms, len(wav_bytes),
        )

        # --- PLAYBACK-POST ---
        logger.info("[PLAYBACK-POST] Sending to client | req=%s | source=%s", request_id, source)
        post_start = time.perf_counter()
        post_to_client(source, wav_bytes)
        post_ms = round((time.perf_counter() - post_start) * 1000)
        logger.info("[PLAYBACK-POST] Done | req=%s | source=%s | %dms", request_id, source, post_ms)

        total_ms = agent_ms + tts_ms + post_ms
        logger.info(
            "[COMPLETE] req=%s | source=%s | agent=%dms tts=%dms post=%dms total=%dms",
            request_id, source, agent_ms, tts_ms, post_ms, total_ms,
        )

    except BridgeError as exc:
        logger.error("[%s] FAILED | req=%s | source=%s | %s", exc.stage, request_id, source, exc.detail)
    except Exception:
        logger.exception("[BACKGROUND] Unexpected error | req=%s | source=%s", request_id, source)


# ---------------------------------------------------------------------------
# Request counter for log correlation
# ---------------------------------------------------------------------------

_request_counter_lock = threading.Lock()
_request_counter = 0


def _next_request_id() -> str:
    global _request_counter
    with _request_counter_lock:
        _request_counter += 1
        return f"voice-{_request_counter:04d}"


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

def _check_auth() -> tuple[Response, int] | None:
    """Verify bearer token. Returns error response or None if OK."""
    try:
        require_env("BRIDGE_TOKEN", BRIDGE_TOKEN)
    except BridgeError as exc:
        logger.error(exc.detail)
        return jsonify({"error": "config", "detail": exc.detail}), 500

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ").strip() != BRIDGE_TOKEN:
        return jsonify({"error": "Unauthorized", "detail": "Missing or invalid bearer token"}), 401

    return None


@app.post("/voice")
def handle_voice() -> tuple[Response, int]:
    """Receive audio, transcribe (sync), then hand off to background thread.

    Returns 202 Accepted with the transcript as soon as STT is done.
    """
    request_id = _next_request_id()

    # --- Auth ---
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    # --- Validate input ---
    uploaded = request.files.get("audio")
    source = (request.form.get("source") or "").strip()

    if not source:
        return jsonify({"error": "Bad request", "detail": "Missing required field: source"}), 400

    if source not in CLIENT_ROUTES:
        return jsonify({
            "error": "Bad request",
            "detail": f"Unknown source '{source}'. Known sources: {list(CLIENT_ROUTES.keys())}",
        }), 400

    if uploaded is None:
        return jsonify({"error": "Bad request", "detail": "Missing multipart field: audio"}), 400

    if not uploaded.filename:
        return jsonify({"error": "Bad request", "detail": "Audio file has no filename"}), 400

    audio_bytes = uploaded.read()
    if not audio_bytes:
        return jsonify({"error": "Bad request", "detail": "Audio file is empty"}), 400

    logger.info(
        "[REQUEST] req=%s | source=%s | filename=%s | bytes=%d",
        request_id, source, uploaded.filename, len(audio_bytes),
    )

    # --- STT (synchronous — fast enough to do before 202) ---
    try:
        logger.info("[STT] Starting | req=%s | source=%s", request_id, source)
        stt_start = time.perf_counter()
        transcript = transcribe_audio(uploaded.filename, audio_bytes)
        stt_ms = round((time.perf_counter() - stt_start) * 1000)
        logger.info(
            '[STT] Done | req=%s | source=%s | %dms | transcript="%s"',
            request_id, source, stt_ms, preview(transcript),
        )
    except BridgeError as exc:
        logger.error("[STT] FAILED | req=%s | source=%s | %s", request_id, source, exc.detail)
        return jsonify({"error": exc.stage, "detail": exc.detail}), 502

    # --- Fire background thread for AGENT → TTS → PLAYBACK-POST ---
    thread = threading.Thread(
        target=_background_reply,
        args=(source, transcript, request_id),
        daemon=True,
    )
    thread.start()

    # --- Return 202 immediately ---
    return jsonify({
        "status": "accepted",
        "request_id": request_id,
        "transcript": transcript,
    }), 202


@app.get("/health")
def health() -> tuple[Response, int]:
    """Simple health check for monitoring."""
    routes_configured = len(CLIENT_ROUTES)
    return jsonify({
        "status": "ok",
        "version": "2.1.0-async",
        "tts_provider": TTS_PROVIDER,
        "routes_configured": routes_configured,
    }), 200


# ---------------------------------------------------------------------------
# Entry point — Waitress WSGI server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting OpenClaw Voice Bridge v2.1 (async) on port %s", BRIDGE_PORT)
    logger.info("TTS provider: %s (voice: %s)", TTS_PROVIDER, GROK_TTS_VOICE if TTS_PROVIDER == "grok" else ELEVENLABS_VOICE_ID)
    logger.info("Configured client routes: %s", list(CLIENT_ROUTES.keys()))

    if not CLIENT_ROUTES:
        logger.warning("No CLIENT_ROUTES configured — replies cannot be delivered!")

    try:
        from waitress import serve
        logger.info("Using Waitress WSGI server")
        serve(app, host="0.0.0.0", port=BRIDGE_PORT, threads=4)
    except ImportError:
        logger.warning("Waitress not installed, falling back to Flask dev server (threaded=True)")
        app.run(host="0.0.0.0", port=BRIDGE_PORT, debug=False, threaded=True)
