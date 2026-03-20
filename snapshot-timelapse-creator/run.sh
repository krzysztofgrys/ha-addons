#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/options.json"

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] Configuration file not found: $CONFIG"
    exit 1
fi

export SNAPSHOT_DIR="$(jq -r '.snapshot_dir // "/homeassistant/timelapse"' "$CONFIG")"
export OUTPUT_DIR="$(jq -r '.output_dir // "/share/timelapses"' "$CONFIG")"
export FILE_PATTERN="$(jq -r '.file_pattern // "*.jpg"' "$CONFIG")"
export MAX_THREADS="$(jq -r '.max_threads // 2' "$CONFIG")"
export BRIGHTNESS_THRESHOLD="$(jq -r '.brightness_threshold // 30' "$CONFIG")"
export NIGHTMODE_THRESHOLD="$(jq -r '.nightmode_threshold // 15' "$CONFIG")"

mkdir -p "$OUTPUT_DIR"

echo "[INFO] Snapshot Timelapse Creator"
echo "[INFO]   Snapshot dir        : ${SNAPSHOT_DIR}"
echo "[INFO]   Output dir          : ${OUTPUT_DIR}"
echo "[INFO]   File pattern        : ${FILE_PATTERN}"
echo "[INFO]   Max threads         : ${MAX_THREADS}"
echo "[INFO]   Brightness threshold: ${BRIGHTNESS_THRESHOLD}"

exec python /app.py
