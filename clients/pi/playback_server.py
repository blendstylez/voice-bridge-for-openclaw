#!/usr/bin/env python3
"""
playback_server.py — Jarvis Voice Loop: Client Playback Server (Pi)

Receives TTS audio (WAV) from the bridge and plays it via aplay.

POST /play
  Authorization: Bearer <PLAYBACK_BEARER_TOKEN>
  Content-Type: audio/wav  (raw bytes)
              OR multipart/form-data with `audio` field
  → 200 {"status":"playing"}

GET /health → 200 {"status":"ok"}
"""

import os
import sys
import subprocess
from datetime import datetime

from flask import Flask, request, jsonify, abort
from dotenv import load_dotenv

load_dotenv()

PLAYBACK_BEARER_TOKEN = os.getenv("PLAYBACK_BEARER_TOKEN")
PLAYBACK_PORT = int(os.getenv("PLAYBACK_PORT", "18780"))
ALSA_DEVICE = os.getenv("ALSA_PLAYBACK_DEVICE", "plughw:3,0")
TEMP_DIR = "/tmp/jarvis"

if not PLAYBACK_BEARER_TOKEN:
    print("[ERROR] PLAYBACK_BEARER_TOKEN must be set in .env")
    sys.exit(1)

os.makedirs(TEMP_DIR, exist_ok=True)

app = Flask(__name__)


def _check_auth():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {PLAYBACK_BEARER_TOKEN}":
        abort(401)


@app.route("/play", methods=["POST"])
def play():
    _check_auth()

    # Accept raw WAV body or multipart with `audio` field
    if request.content_type and "multipart" in request.content_type:
        if "audio" not in request.files:
            return jsonify({"error": "missing audio field"}), 400
        wav_bytes = request.files["audio"].read()
    else:
        wav_bytes = request.data

    if not wav_bytes:
        return jsonify({"error": "empty body"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    wav_path = os.path.join(TEMP_DIR, f"playback_{timestamp}.wav")

    with open(wav_path, "wb") as f:
        f.write(wav_bytes)

    print(f"[PLAY] {len(wav_bytes)} bytes → {wav_path}")
    subprocess.Popen(["aplay", "-q", "-D", ALSA_DEVICE, wav_path])  # non-blocking

    return jsonify({"status": "playing"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0.0", "port": PLAYBACK_PORT}), 200


if __name__ == "__main__":
    print(f"[INIT] Jarvis playback server on 0.0.0.0:{PLAYBACK_PORT}")
    app.run(host="0.0.0.0", port=PLAYBACK_PORT)
