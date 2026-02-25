#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Local test for Audio VAD Recorder
#
# Tests the full pipeline on your machine without Docker or Home Assistant.
# Generates a test WAV with real speech (TTS), feeds it through main.py,
# and verifies that recordings appear in the output directory.
#
# Prerequisites:  ffmpeg, python3, pip packages (torch, torchaudio, numpy)
# On first run it will download the Silero VAD model (~2 MB).
#
# Usage:
#   ./test_local.sh                  # auto-generate speech via TTS
#   ./test_local.sh /path/to/file.wav  # use your own audio file
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR=$(mktemp -d)
OUTPUT_DIR="$TEST_DIR/audio_records"
TEST_WAV="$TEST_DIR/test_input.wav"

cleanup() {
    echo ""
    echo "=== Cleanup ==="
    echo "Test artifacts in: $TEST_DIR"
    echo "(delete manually when done inspecting)"
}
trap cleanup EXIT

echo "=== Audio VAD Recorder – Local Test ==="
echo ""

# -------------------------------------------------------
# 1. Check prerequisites
# -------------------------------------------------------
echo "--- Checking prerequisites ---"
missing=()
command -v ffmpeg  >/dev/null 2>&1 || missing+=("ffmpeg")
command -v python3 >/dev/null 2>&1 || missing+=("python3")

if [ ${#missing[@]} -ne 0 ]; then
    echo "ERROR: Missing required tools: ${missing[*]}"
    exit 1
fi

python3 -c "import torch, numpy" 2>/dev/null || {
    echo "ERROR: Python packages missing. Install with:"
    echo "  pip install torch torchaudio numpy --index-url https://download.pytorch.org/whl/cpu"
    exit 1
}
echo "All prerequisites OK."
echo ""

# -------------------------------------------------------
# 2. Generate or use provided test audio
# -------------------------------------------------------
if [ $# -ge 1 ] && [ -f "$1" ]; then
    echo "--- Using provided audio: $1 ---"
    cp "$1" "$TEST_WAV"
else
    echo "--- Generating test audio with TTS ---"
    TTS_TEXT="Hello. This is a test of voice activity detection. After this sentence there will be silence. And now, a second sentence to verify that multiple segments are recorded correctly."

    if command -v say >/dev/null 2>&1; then
        # macOS
        AIFF_TMP="$TEST_DIR/tts.aiff"
        say -o "$AIFF_TMP" "$TTS_TEXT"
        ffmpeg -y -loglevel error -i "$AIFF_TMP" -ar 16000 -ac 1 "$TEST_WAV"
        rm -f "$AIFF_TMP"
    elif command -v espeak-ng >/dev/null 2>&1; then
        espeak-ng -w "$TEST_WAV" "$TTS_TEXT"
        TMP_RESAMP="$TEST_DIR/resampled.wav"
        ffmpeg -y -loglevel error -i "$TEST_WAV" -ar 16000 -ac 1 "$TMP_RESAMP"
        mv "$TMP_RESAMP" "$TEST_WAV"
    elif command -v espeak >/dev/null 2>&1; then
        espeak -w "$TEST_WAV" "$TTS_TEXT"
        TMP_RESAMP="$TEST_DIR/resampled.wav"
        ffmpeg -y -loglevel error -i "$TEST_WAV" -ar 16000 -ac 1 "$TMP_RESAMP"
        mv "$TMP_RESAMP" "$TEST_WAV"
    else
        echo "No TTS engine found (tried: say, espeak-ng, espeak)."
        echo "Generating a fallback test pattern (silence + tone + silence)."
        echo "NOTE: Sine tones may NOT trigger Silero VAD — this only tests pipeline stability."
        echo "      For a real test, pass a WAV file with speech: ./test_local.sh speech.wav"
        echo ""
        ffmpeg -y -loglevel error \
            -f lavfi -i "anullsrc=r=16000:cl=mono,atrim=duration=2" \
            -f lavfi -i "sine=frequency=300:duration=3,aformat=sample_rates=16000:channel_layouts=mono" \
            -f lavfi -i "anullsrc=r=16000:cl=mono,atrim=duration=3" \
            -f lavfi -i "sine=frequency=400:duration=2,aformat=sample_rates=16000:channel_layouts=mono" \
            -f lavfi -i "anullsrc=r=16000:cl=mono,atrim=duration=2" \
            -filter_complex "[0][1][2][3][4]concat=n=5:v=0:a=1" \
            "$TEST_WAV"
    fi
    echo "Test audio: $TEST_WAV"
    DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TEST_WAV")
    echo "Duration:   ${DURATION}s"
fi
echo ""

# -------------------------------------------------------
# 3. Inject silence gaps (3s) between speech to test
#    multi-segment detection
# -------------------------------------------------------
echo "--- Preparing padded test file ---"
PADDED_WAV="$TEST_DIR/test_padded.wav"
ffmpeg -y -loglevel error \
    -f lavfi -i "anullsrc=r=16000:cl=mono,atrim=duration=2" \
    -i "$TEST_WAV" \
    -f lavfi -i "anullsrc=r=16000:cl=mono,atrim=duration=3" \
    -filter_complex "[0][1][2]concat=n=3:v=0:a=1" \
    "$PADDED_WAV"
TOTAL_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$PADDED_WAV")
echo "Padded audio: $PADDED_WAV (${TOTAL_DURATION}s)"
echo ""

# -------------------------------------------------------
# 4. Run the VAD pipeline
# -------------------------------------------------------
echo "--- Running VAD pipeline ---"
mkdir -p "$OUTPUT_DIR"

export RTSP_URL="$PADDED_WAV"
export VAD_THRESHOLD="0.5"
export CAMERA_NAME="test"
export PRE_PADDING="0.5"
export POST_PADDING="1.5"
export MIN_SPEECH_DURATION="0.3"
export MAX_RECORDING_DURATION="60"

TIMEOUT_S=$(python3 -c "print(int(float('${TOTAL_DURATION}') + 30))")

# Patch OUTPUT_DIR for local testing (writes to temp dir instead of /media)
python3 -c "
import sys, os
sys.path.insert(0, '${SCRIPT_DIR}')

import main as m
from pathlib import Path

m.OUTPUT_DIR = Path('${OUTPUT_DIR}')
m.RECONNECT_DELAY_S = 0

m.main()
" &
PID=$!

echo "Pipeline PID: $PID"
echo "Waiting up to ${TIMEOUT_S}s for processing..."

# Wait for the process, but kill if it takes too long
if ! timeout "$TIMEOUT_S" tail --pid="$PID" -f /dev/null 2>/dev/null; then
    # GNU timeout not available (macOS), fallback
    SECONDS_WAITED=0
    while kill -0 "$PID" 2>/dev/null && [ "$SECONDS_WAITED" -lt "$TIMEOUT_S" ]; do
        sleep 1
        SECONDS_WAITED=$((SECONDS_WAITED + 1))
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "Timeout – killing pipeline."
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
    fi
fi

wait "$PID" 2>/dev/null || true
echo ""

# -------------------------------------------------------
# 5. Check results
# -------------------------------------------------------
echo "=== Results ==="
echo "Output directory: $OUTPUT_DIR"
echo ""

WAV_COUNT=$(find "$OUTPUT_DIR" -name "*.wav" 2>/dev/null | wc -l | tr -d ' ')

if [ "$WAV_COUNT" -gt 0 ]; then
    echo "SUCCESS: $WAV_COUNT recording(s) created:"
    echo ""
    for f in "$OUTPUT_DIR"/*.wav; do
        DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
        SIZE=$(du -h "$f" | cut -f1)
        echo "  $(basename "$f")  —  ${DUR}s  ($SIZE)"
    done
    echo ""
    echo "Listen to them:"
    for f in "$OUTPUT_DIR"/*.wav; do
        echo "  open \"$f\"        # macOS"
        echo "  aplay \"$f\"       # Linux"
    done
else
    echo "NO recordings were created."
    echo ""
    echo "This can happen if:"
    echo "  1. The test audio didn't contain speech (sine-tone fallback)"
    echo "  2. The VAD threshold is too high (try 0.3)"
    echo "  3. There's a bug in the pipeline"
    echo ""
    echo "Try again with a real speech file:"
    echo "  ./test_local.sh /path/to/speech.wav"
fi
echo ""
