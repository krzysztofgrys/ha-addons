#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/options.json"

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] Configuration file not found: $CONFIG"
    exit 1
fi

export RTSP_URL=$(jq -r '.rtsp_url' "$CONFIG")
export CAMERA_NAME=$(jq -r '.camera_name // "kamera"' "$CONFIG")
export VAD_THRESHOLD=$(jq -r '.vad_threshold // 0.7' "$CONFIG")
export PRE_PADDING=$(jq -r '.pre_padding // 0.5' "$CONFIG")
export POST_PADDING=$(jq -r '.post_padding // 1.5' "$CONFIG")
export MIN_SPEECH_DURATION=$(jq -r '.min_speech_duration // 0.5' "$CONFIG")
export MAX_RECORDING_DURATION=$(jq -r '.max_recording_duration // 300' "$CONFIG")

if [ -z "$RTSP_URL" ] || [ "$RTSP_URL" = "null" ]; then
    echo "[ERROR] rtsp_url is not configured. Set it in the add-on options."
    exit 1
fi

echo "[INFO] Starting Audio VAD Recorder"
echo "[INFO]   Camera         : ${CAMERA_NAME}"
echo "[INFO]   VAD Threshold  : ${VAD_THRESHOLD}"
echo "[INFO]   Pre-padding    : ${PRE_PADDING}s"
echo "[INFO]   Post-padding   : ${POST_PADDING}s"
echo "[INFO]   Min speech     : ${MIN_SPEECH_DURATION}s"
echo "[INFO]   Max recording  : ${MAX_RECORDING_DURATION}s"

exec python /main.py
