#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/options.json"

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] Configuration file not found: $CONFIG"
    exit 1
fi

export UNIFI_BASE_URL="$(jq -r '.unifi_base_url // ""' "$CONFIG")"
export UNIFI_API_KEY="$(jq -r '.unifi_api_key // ""' "$CONFIG")"
export CAMERA_ID="$(jq -r '.camera_id // ""' "$CONFIG")"
export HOURS_BACK="$(jq -r '.hours_back // 6' "$CONFIG")"
export OPENAI_API_KEY="$(jq -r '.openai_api_key // ""' "$CONFIG")"
export WHISPER_MODEL="$(jq -r '.whisper_model // "whisper-1"' "$CONFIG")"
export WHISPER_LANGUAGE="$(jq -r '.whisper_language // "pl"' "$CONFIG")"
export SILENCE_THRESHOLD_DB="$(jq -r '.silence_threshold_db // "-40dB"' "$CONFIG")"
export START_SILENCE_DURATION="$(jq -r '.start_silence_duration // 0.2' "$CONFIG")"
export STOP_SILENCE_DURATION="$(jq -r '.stop_silence_duration // 0.5' "$CONFIG")"
export VERIFY_TLS="$(jq -r '.verify_tls // false' "$CONFIG")"

if [ -z "$UNIFI_BASE_URL" ] || [ "$UNIFI_BASE_URL" = "null" ]; then
    echo "[ERROR] unifi_base_url is not configured."
    exit 1
fi
if [ -z "$UNIFI_API_KEY" ] || [ "$UNIFI_API_KEY" = "null" ]; then
    echo "[ERROR] unifi_api_key is not configured."
    exit 1
fi
if [ -z "$CAMERA_ID" ] || [ "$CAMERA_ID" = "null" ]; then
    echo "[ERROR] camera_id is not configured."
    exit 1
fi
if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "null" ]; then
    echo "[ERROR] openai_api_key is not configured."
    exit 1
fi

echo "[INFO] Starting UniFi Protect Historical Transcriber"
echo "[INFO]   UniFi URL      : ${UNIFI_BASE_URL}"
echo "[INFO]   Camera ID      : ${CAMERA_ID}"
echo "[INFO]   Hours back     : ${HOURS_BACK}"
echo "[INFO]   Whisper model  : ${WHISPER_MODEL}"
echo "[INFO]   Whisper lang   : ${WHISPER_LANGUAGE}"

exec python /main.py
