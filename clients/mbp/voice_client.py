#!/usr/bin/env python3
"""
voice_client.py — Jarvis Voice Loop: Client (async v2, macOS)

Flow: [LISTENING] → wake word → [RECORDING] → silence/max → [UPLOADED] → [LISTENING]

Reply arrives later via playback_server.py (POST /play from the bridge).

Requires a .env file in the same directory with:
    OPENCLAW_WEBHOOK_URL=http://<mac-mini-lan-ip>:18790/voice
    OPENCLAW_BEARER_TOKEN=<shared-secret>
    CLIENT_SOURCE_NAME=mbp-dev
"""

import os
import sys
import time
import wave
import struct
import signal
import math
from datetime import datetime

import numpy as np
import pyaudio
import requests
from dotenv import load_dotenv
from openwakeword.model import Model

# ---------------------------------------------------------------------------
# Config — tune these if needed
# ---------------------------------------------------------------------------
WAKE_THRESHOLD      = 0.5       # confidence score to trigger (0–1)
SAMPLE_RATE         = 16000     # Hz — Whisper-compatible
CHANNELS            = 1
SAMPLE_WIDTH        = 2         # bytes, paInt16
CHUNK               = 1280      # samples per frame (80ms at 16kHz)

RECORD_MAX_SECONDS  = 10        # hard cap on recording length
SILENCE_SECONDS     = 2.5       # seconds of silence that ends recording early
SILENCE_RMS_THRESH  = 400       # RMS below this = silence (tune if needed)

MIN_RECORD_SECONDS  = 1.5       # recordings shorter than this are dropped (phantom filter)
POST_UPLOAD_COOLDOWN = 5.0      # seconds to ignore wake word after an upload (phantom filter)

HTTP_TIMEOUT        = 10        # seconds to wait for 202 ack from bridge (upload only)

TEMP_DIR            = "/tmp/jarvis"   # debug WAV files land here

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
load_dotenv()

WEBHOOK_URL   = os.getenv("OPENCLAW_WEBHOOK_URL")
BEARER_TOKEN  = os.getenv("OPENCLAW_BEARER_TOKEN")
SOURCE_NAME   = os.getenv("CLIENT_SOURCE_NAME", "mbp-dev")

if not WEBHOOK_URL or not BEARER_TOKEN:
    print("[ERROR] OPENCLAW_WEBHOOK_URL and OPENCLAW_BEARER_TOKEN must be set in .env")
    sys.exit(1)

os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Graceful shutdown on Ctrl+C
# ---------------------------------------------------------------------------
_running = True

def _handle_sigint(sig, frame):
    global _running
    print("\n[EXIT] Ctrl+C received — shutting down cleanly.")
    _running = False

signal.signal(signal.SIGINT, _handle_sigint)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rms(chunk_bytes: bytes) -> float:
    """Return RMS amplitude of a raw int16 PCM chunk."""
    count = len(chunk_bytes) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", chunk_bytes)
    sum_sq = sum(s * s for s in shorts)
    result = math.sqrt(sum_sq / count)
#    print(f"  [rms] {result:.0f}")
    return result

def save_wav(frames: list, path: str) -> None:
    """Write raw PCM frames to a WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))


def record_until_silence_or_max(mic_stream: pyaudio.Stream, prefix: bytes = b"") -> str:
    """
    Record audio until either:
      - RECORD_MAX_SECONDS elapsed, or
      - SILENCE_SECONDS of consecutive silence detected

    prefix: raw PCM bytes already read from the buffer (so the first
            chunk of speech isn't lost after wake-word detection).

    Returns path to saved WAV file.
    """
    print("[RECORDING]")
    frames = [prefix] if prefix else []
    silence_chunks = 0
    silence_chunk_limit = int(SILENCE_SECONDS * SAMPLE_RATE / CHUNK)
    max_chunks = int(RECORD_MAX_SECONDS * SAMPLE_RATE / CHUNK)

    for _ in range(max_chunks):
        if not _running:
            break
        data = mic_stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)

        if rms(data) < SILENCE_RMS_THRESH:
            silence_chunks += 1
        else:
            silence_chunks = 0

        if silence_chunks >= silence_chunk_limit:
            print(f"[RECORDING] silence detected — stopping early")
            break

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(TEMP_DIR, f"jarvis_turn_{timestamp}.wav")
    save_wav(frames, wav_path)
    duration = len(frames) * CHUNK / SAMPLE_RATE
    print(f"[RECORDING] saved {len(frames)} chunks ({duration:.1f}s) → {wav_path}")
    return wav_path, duration


def send_to_bridge(wav_path: str) -> bool:
    """
    POST WAV to the voice bridge.
    Expects 202 Accepted. Returns True on success, False on any error.
    """
    print("[SENDING]")
    try:
        with open(wav_path, "rb") as f:
            response = requests.post(
                WEBHOOK_URL,
                headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
                files={"audio": ("audio.wav", f, "audio/wav")},
                data={"source": SOURCE_NAME},
                timeout=HTTP_TIMEOUT,
            )
        if response.status_code == 202:
            print(f"[UPLOADED] 202 Accepted — bridge processing in background")
            return True
        else:
            print(f"[ERROR] bridge returned HTTP {response.status_code}: {response.text[:300]}")
            return False
    except requests.exceptions.Timeout:
        print(f"[ERROR] upload timed out after {HTTP_TIMEOUT}s")
        return False
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] network error: {e}")
        return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print("[INIT] Checking/downloading openWakeWord models…")
    import openwakeword
    openwakeword.utils.download_models()
    print("[INIT] Loading openWakeWord model (hey_jarvis)…")
    oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    print("[INIT] Model loaded.")

    pa = pyaudio.PyAudio()
    mic = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )

    print(f"[LISTENING] Waiting for 'Hey Jarvis'… (threshold={WAKE_THRESHOLD})")
    print("            Press Ctrl+C to exit.\n")

    _last_upload_time = 0.0

    try:
        while _running:
            # --- Wake word detection ---
            audio_chunk = np.frombuffer(
                mic.read(CHUNK, exception_on_overflow=False), dtype=np.int16
            )
            prediction = oww.predict(audio_chunk)

            for model_name, score in prediction.items():
                if score >= WAKE_THRESHOLD:
                    # --- Cooldown check (phantom filter) ---
                    cooldown_remaining = POST_UPLOAD_COOLDOWN - (time.time() - _last_upload_time)
                    if cooldown_remaining > 0:
                        print(f"[COOLDOWN] wake ignored (score={score:.3f}) — {cooldown_remaining:.1f}s remaining")
                        oww.reset()
                        break

                    print(f"\n[WAKE DETECTED] '{model_name}' score={score:.3f}")

                    # Keep buffered audio as the start of the recording
                    # (dropping it causes the first ~200ms of speech to be cut off)
                    buffered = mic.read(mic.get_read_available(), exception_on_overflow=False)

                    # Record the user's command, passing buffered audio as prefix
                    wav_path, duration = record_until_silence_or_max(mic, prefix=buffered)

                    # --- Minimum length check (phantom filter) ---
                    if duration < MIN_RECORD_SECONDS:
                        print(f"[DROPPED] recording too short ({duration:.1f}s < {MIN_RECORD_SECONDS}s) — likely phantom")
                        oww.reset()
                        print(f"\n[LISTENING] Waiting for 'Hey Jarvis'…")
                        break

                    # Upload to bridge (fire-and-forget — reply comes via playback_server.py)
                    if send_to_bridge(wav_path):
                        _last_upload_time = time.time()

                    # Reset oww buffer so speech just recorded doesn't re-trigger
                    oww.reset()

                    print(f"\n[LISTENING] Waiting for 'Hey Jarvis'…")
                    break  # back to outer while loop

    finally:
        print("[EXIT] Closing mic…")
        mic.stop_stream()
        mic.close()
        pa.terminate()
        print("[EXIT] Done.")


if __name__ == "__main__":
    main()
