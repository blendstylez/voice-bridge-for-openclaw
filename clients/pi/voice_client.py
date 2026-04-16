#!/usr/bin/env python3
"""
voice_client.py — Jarvis Voice Loop: Client (async v2, Pi port)

Flow: [LISTENING] → wake word → [RECORDING] → silence/max → [UPLOADED] → [LISTENING]

Reply arrives later via playback_server.py (POST /play from the bridge).

Requires a .env file in the same directory with:
    OPENCLAW_WEBHOOK_URL=http://<mac-mini-lan-ip>:18790/voice
    OPENCLAW_BEARER_TOKEN=<shared-secret>
    CLIENT_SOURCE_NAME=pi-livingroom
    INPUT_DEVICE_INDEX=<pyaudio index of USB mic>

Pi note: USB mic is opened at its native rate (48kHz, stereo).
         Audio is converted to 16kHz mono in Python before feeding openWakeWord.
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
# Config
# ---------------------------------------------------------------------------
SAMPLE_RATE         = 16000     # Hz — for openWakeWord and output WAV
CHANNELS            = 1         # mono output
SAMPLE_WIDTH        = 2         # bytes, paInt16

# Native USB mic settings on Pi (only supports 48000Hz stereo)
NATIVE_SAMPLE_RATE  = 48000
NATIVE_CHANNELS     = 2
DOWNSAMPLE_RATIO    = NATIVE_SAMPLE_RATE // SAMPLE_RATE  # = 3

CHUNK               = 1280      # samples at 16kHz (80ms) — for openWakeWord
CHUNK_NATIVE        = CHUNK * DOWNSAMPLE_RATIO  # samples at 48kHz to read

WAKE_THRESHOLD      = 0.5
RECORD_MAX_SECONDS  = 10
SILENCE_SECONDS     = 2.5
SILENCE_RMS_THRESH  = 400

MIN_RECORD_SECONDS  = 1.5       # recordings shorter than this are dropped (phantom filter)
POST_UPLOAD_COOLDOWN = 5.0      # seconds to ignore wake word after an upload (phantom filter)

HTTP_TIMEOUT        = 10
TEMP_DIR            = "/tmp/jarvis"

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
load_dotenv()

WEBHOOK_URL        = os.getenv("OPENCLAW_WEBHOOK_URL")
BEARER_TOKEN       = os.getenv("OPENCLAW_BEARER_TOKEN")
SOURCE_NAME        = os.getenv("CLIENT_SOURCE_NAME", "pi-livingroom")
INPUT_DEVICE_INDEX = int(os.getenv("INPUT_DEVICE_INDEX", "-1"))

if not WEBHOOK_URL or not BEARER_TOKEN:
    print("[ERROR] OPENCLAW_WEBHOOK_URL and OPENCLAW_BEARER_TOKEN must be set in .env")
    sys.exit(1)

os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True

def _handle_sigint(sig, frame):
    global _running
    print("\n[EXIT] Ctrl+C received — shutting down cleanly.")
    _running = False

signal.signal(signal.SIGINT, _handle_sigint)

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def convert_native_to_16k_mono(raw_bytes: bytes) -> bytes:
    """Convert 48kHz stereo int16 to 16kHz mono int16 (decimate 3:1)."""
    arr = np.frombuffer(raw_bytes, dtype=np.int16).reshape(-1, NATIVE_CHANNELS)
    mono = arr.mean(axis=1).astype(np.int16)   # stereo → mono
    decimated = mono[::DOWNSAMPLE_RATIO]         # 48kHz → 16kHz
    return decimated.tobytes()

def rms(chunk_bytes: bytes) -> float:
    """Return RMS amplitude of a raw int16 PCM chunk."""
    count = len(chunk_bytes) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", chunk_bytes)
    sum_sq = sum(s * s for s in shorts)
    return math.sqrt(sum_sq / count)

def save_wav(frames: list, path: str) -> None:
    """Write raw PCM frames (16kHz mono int16) to a WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))


def record_until_silence_or_max(mic_stream: pyaudio.Stream, prefix: bytes = b"") -> str:
    """
    Record audio until either RECORD_MAX_SECONDS elapsed or SILENCE_SECONDS of silence.
    prefix: already-converted 16kHz mono bytes from the buffer at wake detection.
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
        raw = mic_stream.read(CHUNK_NATIVE, exception_on_overflow=False)
        data = convert_native_to_16k_mono(raw)
        frames.append(data)

        if rms(data) < SILENCE_RMS_THRESH:
            silence_chunks += 1
        else:
            silence_chunks = 0

        if silence_chunks >= silence_chunk_limit:
            print("[RECORDING] silence detected — stopping early")
            break

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(TEMP_DIR, f"jarvis_turn_{timestamp}.wav")
    save_wav(frames, wav_path)
    duration = len(frames) * CHUNK / SAMPLE_RATE
    print(f"[RECORDING] saved {len(frames)} chunks ({duration:.1f}s) → {wav_path}")
    return wav_path, duration


def send_to_bridge(wav_path: str) -> bool:
    """POST WAV to the voice bridge. Expects 202 Accepted."""
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
            print("[UPLOADED] 202 Accepted — bridge processing in background")
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
    print("[INIT] Loading openWakeWord model (hey_jarvis)…")
    import openwakeword as _oww_pkg
    import os as _os
    _model_path = _os.path.join(_os.path.dirname(_oww_pkg.__file__), "resources", "models", "hey_jarvis_v0.1.onnx")
    oww = Model(wakeword_models=[_model_path], inference_framework="onnx")
    print("[INIT] Model loaded.")

    pa = pyaudio.PyAudio()

    device_index = INPUT_DEVICE_INDEX if INPUT_DEVICE_INDEX >= 0 else None
    print(f"[INIT] Opening mic device={device_index} at {NATIVE_SAMPLE_RATE}Hz/{NATIVE_CHANNELS}ch (→ downsampled to {SAMPLE_RATE}Hz mono)")
    mic = pa.open(
        format=pyaudio.paInt16,
        channels=NATIVE_CHANNELS,
        rate=NATIVE_SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_NATIVE,
        input_device_index=device_index,
    )

    print(f"[LISTENING] Waiting for 'Hey Jarvis'… (threshold={WAKE_THRESHOLD})")
    print("            Press Ctrl+C to exit.\n")

    _last_upload_time = 0.0

    try:
        while _running:
            # Read native audio and convert to 16kHz mono for openWakeWord
            raw_chunk = mic.read(CHUNK_NATIVE, exception_on_overflow=False)
            audio_chunk = np.frombuffer(
                convert_native_to_16k_mono(raw_chunk), dtype=np.int16
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

                    # Read buffered audio and convert it too
                    available = mic.get_read_available()
                    raw_buffered = mic.read(available, exception_on_overflow=False) if available > 0 else b""
                    buffered_converted = convert_native_to_16k_mono(raw_buffered) if raw_buffered else b""

                    wav_path, duration = record_until_silence_or_max(mic, prefix=buffered_converted)

                    # --- Minimum length check (phantom filter) ---
                    if duration < MIN_RECORD_SECONDS:
                        print(f"[DROPPED] recording too short ({duration:.1f}s < {MIN_RECORD_SECONDS}s) — likely phantom")
                        oww.reset()
                        print(f"\n[LISTENING] Waiting for 'Hey Jarvis'…")
                        break

                    if send_to_bridge(wav_path):
                        _last_upload_time = time.time()
                    oww.reset()

                    print(f"\n[LISTENING] Waiting for 'Hey Jarvis'…")
                    break

    finally:
        print("[EXIT] Closing mic…")
        mic.stop_stream()
        mic.close()
        pa.terminate()
        print("[EXIT] Done.")


if __name__ == "__main__":
    main()
