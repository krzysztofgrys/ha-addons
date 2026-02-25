import os
import sys
import signal
import subprocess
import threading
import wave
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Queue

import numpy as np
import onnxruntime as ort

# ---------------------------------------------------------------------------
# Configuration from environment (set by run.sh from /data/options.json)
# ---------------------------------------------------------------------------
RTSP_URL = os.environ.get("RTSP_URL", "")
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.7"))
CAMERA_NAME = os.environ.get("CAMERA_NAME", "kamera")

PRE_PADDING_S = float(os.environ.get("PRE_PADDING", "0.5"))
POST_PADDING_S = float(os.environ.get("POST_PADDING", "1.5"))
MIN_SPEECH_S = float(os.environ.get("MIN_SPEECH_DURATION", "0.5"))
MAX_RECORDING_S = int(os.environ.get("MAX_RECORDING_DURATION", "300"))

WHISPER_ENABLED = os.environ.get("WHISPER_ENABLED", "false").lower() == "true"
WHISPER_API_URL = os.environ.get("WHISPER_API_URL", "")
WHISPER_API_KEY = os.environ.get("WHISPER_API_KEY", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "pl")

# ---------------------------------------------------------------------------
# Derived constants (not user-configurable)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512          # 32 ms at 16 kHz – the ONLY size supported by Silero ONNX model
BYTES_PER_SAMPLE = 2         # s16le
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE
CHUNK_DURATION_S = CHUNK_SAMPLES / SAMPLE_RATE

POST_PADDING_CHUNKS = int(POST_PADDING_S / CHUNK_DURATION_S)
PRE_PADDING_CHUNKS = max(1, int(PRE_PADDING_S / CHUNK_DURATION_S))

MIN_SPEECH_SAMPLES = int(MIN_SPEECH_S * SAMPLE_RATE)
MAX_RECORDING_BYTES = int(MAX_RECORDING_S * SAMPLE_RATE * BYTES_PER_SAMPLE)

IDLE_RESET_CHUNKS = int(30.0 / CHUNK_DURATION_S)

OUTPUT_DIR = Path("/media/audio_records")
RECONNECT_DELAY_S = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("vad")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
running = True


def _shutdown(sig, _frame):
    global running
    log.info("Received signal %s – shutting down …", sig)
    running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# ---------------------------------------------------------------------------
# Whisper transcription (runs in a separate thread)
# ---------------------------------------------------------------------------
transcription_queue: Queue[Path] = Queue()

_SENTINEL = None


def _transcribe_worker():
    """Daemon thread: picks WAV files from queue and sends them to Whisper API."""
    import requests

    session = requests.Session()
    if WHISPER_API_KEY:
        session.headers["Authorization"] = f"Bearer {WHISPER_API_KEY}"

    while True:
        item = transcription_queue.get()
        if item is _SENTINEL:
            break

        wav_path: Path = item
        txt_path = wav_path.with_suffix(".txt")

        try:
            with open(wav_path, "rb") as f:
                files = {"file": (wav_path.name, f, "audio/wav")}
                data = {"model": WHISPER_MODEL, "language": WHISPER_LANGUAGE}
                resp = session.post(WHISPER_API_URL, files=files, data=data, timeout=30)

            resp.raise_for_status()
            text = resp.json().get("text", "").strip()

            txt_path.write_text(text, encoding="utf-8")
            log.info("Transcription [%s]: %s", wav_path.name, text[:120])

        except requests.exceptions.ConnectionError:
            log.error("Whisper API unreachable: %s", WHISPER_API_URL)
        except requests.exceptions.Timeout:
            log.error("Whisper API timeout for %s", wav_path.name)
        except Exception:
            log.exception("Transcription failed for %s", wav_path.name)
        finally:
            transcription_queue.task_done()


def start_transcription_worker() -> threading.Thread:
    t = threading.Thread(target=_transcribe_worker, daemon=True, name="whisper")
    t.start()
    log.info("Whisper transcription worker started.")
    return t


def stop_transcription_worker():
    transcription_queue.put(_SENTINEL)


# ---------------------------------------------------------------------------
# Silero VAD via ONNX Runtime
# ---------------------------------------------------------------------------

SILERO_MODEL_PATH = os.environ.get("SILERO_MODEL_PATH", "/opt/silero/silero_vad.onnx")


class SileroVAD:
    """Lightweight wrapper around the Silero VAD ONNX model."""

    def __init__(self, model_path: str):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(model_path, sess_options=opts)

        self.reset_states()
        log.info("Silero VAD ONNX model loaded from %s", model_path)

    def reset_states(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, audio_chunk: np.ndarray) -> float:
        ort_inputs = {
            "input": audio_chunk.reshape(1, -1),
            "sr": np.array([SAMPLE_RATE], dtype=np.int64),
            "state": self._state,
        }
        out, self._state = self._session.run(None, ort_inputs)
        return float(out.squeeze())


def load_vad_model() -> SileroVAD:
    path = SILERO_MODEL_PATH
    if not os.path.isfile(path):
        log.error("ONNX model not found at %s", path)
        sys.exit(1)
    return SileroVAD(path)


# ---------------------------------------------------------------------------
# FFmpeg & audio helpers
# ---------------------------------------------------------------------------


def _stderr_drain(pipe):
    """Daemon thread: reads FFmpeg stderr so the pipe buffer never fills up."""
    try:
        for line in pipe:
            log.warning("FFmpeg: %s", line.decode(errors="replace").rstrip())
    except Exception:
        pass
    finally:
        pipe.close()


def start_ffmpeg(rtsp_url: str) -> subprocess.Popen:
    is_rtsp = rtsp_url.lower().startswith(("rtsp://", "rtsps://"))
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if is_rtsp:
        cmd += ["-rtsp_transport", "tcp", "-flags", "low_delay", "-fflags", "nobuffer"]
    cmd += [
        "-i", rtsp_url,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-f", "s16le",
        "pipe:1",
    ]
    log.info("FFmpeg command: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    t = threading.Thread(target=_stderr_drain, args=(proc.stderr,), daemon=True)
    t.start()
    return proc


def pcm_to_float(pcm_bytes: bytes) -> np.ndarray:
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def save_wav(pcm_data: bytearray, filepath: Path):
    with wave.open(str(filepath), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(BYTES_PER_SAMPLE)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    duration = len(pcm_data) / BYTES_PER_SAMPLE / SAMPLE_RATE
    log.info("Saved %s  (%.1f s)", filepath.name, duration)


def _close_ffmpeg(proc: subprocess.Popen):
    """Terminate/kill FFmpeg and close the stdout pipe to free the FD."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    try:
        proc.stdout.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------


def _flush_recording(speech_buffer: bytearray, model: SileroVAD, reason: str):
    """Save the current recording buffer, queue transcription, reset state."""
    total_samples = len(speech_buffer) // BYTES_PER_SAMPLE
    if total_samples >= MIN_SPEECH_SAMPLES:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = OUTPUT_DIR / f"{CAMERA_NAME}_{ts}.wav"
        save_wav(speech_buffer, filepath)
        log.info("Flush reason: %s", reason)

        if WHISPER_ENABLED:
            transcription_queue.put(filepath)
    else:
        log.debug("Discarding short segment (%.2f s).", total_samples / SAMPLE_RATE)
    speech_buffer.clear()
    model.reset_states()


def process_stream(model: SileroVAD):
    global running

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = start_ffmpeg(RTSP_URL)

    pre_buffer: deque[bytes] = deque(maxlen=PRE_PADDING_CHUNKS)
    speech_buffer = bytearray()
    silence_count = 0
    idle_count = 0
    is_recording = False

    try:
        while running:
            raw = ffmpeg.stdout.read(CHUNK_BYTES)

            if not raw or len(raw) < CHUNK_BYTES:
                log.warning("FFmpeg stream ended or incomplete read.")
                break

            audio = pcm_to_float(raw)
            speech_prob = model(audio)

            if speech_prob >= VAD_THRESHOLD:
                idle_count = 0

                if not is_recording:
                    log.info(
                        "Speech started (prob=%.2f, threshold=%.2f)",
                        speech_prob,
                        VAD_THRESHOLD,
                    )
                    is_recording = True
                    for chunk in pre_buffer:
                        speech_buffer.extend(chunk)
                    pre_buffer.clear()

                speech_buffer.extend(raw)
                silence_count = 0

                if len(speech_buffer) >= MAX_RECORDING_BYTES:
                    _flush_recording(speech_buffer, model, "max duration reached")
                    is_recording = False

            else:
                if is_recording:
                    speech_buffer.extend(raw)
                    silence_count += 1

                    if silence_count >= POST_PADDING_CHUNKS:
                        _flush_recording(speech_buffer, model, "silence timeout")
                        silence_count = 0
                        is_recording = False
                        idle_count = 0
                else:
                    pre_buffer.append(raw)
                    idle_count += 1
                    if idle_count >= IDLE_RESET_CHUNKS:
                        model.reset_states()
                        idle_count = 0

    finally:
        if is_recording and len(speech_buffer) > 0:
            _flush_recording(speech_buffer, model, "stream ended while recording")
        _close_ffmpeg(ffmpeg)


# ---------------------------------------------------------------------------
# Entry point with auto-reconnect
# ---------------------------------------------------------------------------


def main():
    global running

    if not RTSP_URL:
        log.error("RTSP URL is not set – configure it in the add-on options.")
        sys.exit(1)

    log.info("Configuration:")
    log.info("  VAD threshold     : %.2f", VAD_THRESHOLD)
    log.info("  Pre-padding       : %.2f s  (%d chunks)", PRE_PADDING_S, PRE_PADDING_CHUNKS)
    log.info("  Post-padding      : %.2f s  (%d chunks)", POST_PADDING_S, POST_PADDING_CHUNKS)
    log.info("  Min speech        : %.2f s", MIN_SPEECH_S)
    log.info("  Max recording     : %d s", MAX_RECORDING_S)
    log.info("  Output dir        : %s", OUTPUT_DIR)
    log.info("  Whisper           : %s", "enabled" if WHISPER_ENABLED else "disabled")
    if WHISPER_ENABLED:
        log.info("  Whisper API       : %s", WHISPER_API_URL)
        log.info("  Whisper model     : %s", WHISPER_MODEL)
        log.info("  Whisper language  : %s", WHISPER_LANGUAGE)
        start_transcription_worker()

    model = load_vad_model()

    while running:
        try:
            process_stream(model)
        except Exception:
            log.exception("Unexpected error in processing loop.")

        if not running:
            break

        log.info("Reconnecting in %d s …", RECONNECT_DELAY_S)
        for _ in range(RECONNECT_DELAY_S * 10):
            if not running:
                break
            time.sleep(0.1)

        model.reset_states()

    if WHISPER_ENABLED:
        log.info("Waiting for pending transcriptions …")
        transcription_queue.join()
        stop_transcription_worker()

    log.info("Audio VAD Recorder stopped.")


if __name__ == "__main__":
    main()
