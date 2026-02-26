import logging
import os
import subprocess
import sys
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


UNIFI_BASE_URL = os.environ.get("UNIFI_BASE_URL", "").rstrip("/")
UNIFI_API_KEY = os.environ.get("UNIFI_API_KEY", "")
CAMERA_ID = os.environ.get("CAMERA_ID", "")
HOURS_BACK = int(os.environ.get("HOURS_BACK", "6"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "pl")
SILENCE_THRESHOLD_DB = os.environ.get("SILENCE_THRESHOLD_DB", "-40dB")
START_SILENCE_DURATION = float(os.environ.get("START_SILENCE_DURATION", "0.2"))
STOP_SILENCE_DURATION = float(os.environ.get("STOP_SILENCE_DURATION", "0.5"))
VERIFY_TLS = os.environ.get("VERIFY_TLS", "false").lower() == "true"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

UNIFI_EXPORT_PATH = "/proxy/protect/api/video/export"
WHISPER_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
HA_NOTIFY_ENDPOINT = "http://supervisor/core/api/services/persistent_notification/create"
CHUNK_DURATION = timedelta(hours=1)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("unifi-historical-transcriber")


def validate_config() -> None:
    if not UNIFI_BASE_URL:
        raise ValueError("UNIFI_BASE_URL is empty")
    if not UNIFI_API_KEY:
        raise ValueError("UNIFI_API_KEY is empty")
    if not CAMERA_ID:
        raise ValueError("CAMERA_ID is empty")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is empty")
    if HOURS_BACK <= 0:
        raise ValueError("HOURS_BACK must be > 0")


def build_hour_chunks(hours_back: int):
    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(hours=hours_back)

    chunks = []
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(cursor + CHUNK_DURATION, end_ts)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def download_chunk_mp4(
    session: requests.Session, chunk_start: datetime, chunk_end: datetime, output_path: Path
) -> bool:
    url = f"{UNIFI_BASE_URL}{UNIFI_EXPORT_PATH}"
    params = {
        "camera": CAMERA_ID,
        "start": int(chunk_start.timestamp() * 1000),
        "end": int(chunk_end.timestamp() * 1000),
    }
    headers = {"X-API-Key": UNIFI_API_KEY}

    log.info(
        "Downloading chunk %s -> %s",
        chunk_start.isoformat(),
        chunk_end.isoformat(),
    )
    with session.get(
        url,
        headers=headers,
        params=params,
        stream=True,
        timeout=(10, 600),
        verify=VERIFY_TLS,
    ) as resp:
        if resp.status_code >= 400:
            log.error("UniFi API error %s: %s", resp.status_code, resp.text[:300])
            return False

        with open(output_path, "wb") as f:
            for piece in resp.iter_content(chunk_size=1024 * 1024):
                if piece:
                    f.write(piece)

    if not output_path.exists() or output_path.stat().st_size == 0:
        log.warning("Downloaded MP4 is empty for chunk %s", chunk_start.isoformat())
        return False

    return True


def extract_wav_with_silence_removal(mp4_path: Path, wav_path: Path) -> bool:
    filter_expr = (
        f"silenceremove="
        f"start_periods=1:"
        f"start_duration={START_SILENCE_DURATION}:"
        f"start_threshold={SILENCE_THRESHOLD_DB}:"
        f"stop_periods=-1:"
        f"stop_duration={STOP_SILENCE_DURATION}:"
        f"stop_threshold={SILENCE_THRESHOLD_DB}"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp4_path),
        "-vn",
        "-af",
        filter_expr,
        "-ac",
        "1",
        "-ar",
        "16000",
        str(wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg failed for %s: %s", mp4_path.name, result.stderr.strip())
        return False

    if not wav_path.exists() or wav_path.stat().st_size <= 44:
        log.info("No speech found after silenceremove for %s", mp4_path.name)
        return False

    return True


def merge_wavs(wav_paths, merged_path: Path) -> bool:
    if not wav_paths:
        return False

    with wave.open(str(merged_path), "wb") as out_wav:
        params = None
        for idx, src in enumerate(wav_paths):
            with wave.open(str(src), "rb") as in_wav:
                if idx == 0:
                    params = in_wav.getparams()
                    out_wav.setparams(params)
                elif in_wav.getparams()[:4] != params[:4]:
                    log.error("WAV format mismatch while merging: %s", src.name)
                    return False
                out_wav.writeframes(in_wav.readframes(in_wav.getnframes()))

    return merged_path.exists() and merged_path.stat().st_size > 44


def transcribe_with_openai(audio_path: Path) -> str:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = {"model": WHISPER_MODEL, "language": WHISPER_LANGUAGE}
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path.name, f, "audio/wav")}
        response = requests.post(
            WHISPER_ENDPOINT,
            headers=headers,
            data=data,
            files=files,
            timeout=300,
        )
    response.raise_for_status()
    return response.json().get("text", "").strip()


def send_home_assistant_notification(message: str, title: str) -> None:
    if not SUPERVISOR_TOKEN:
        log.warning("SUPERVISOR_TOKEN is missing, skipping HA notification.")
        return

    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"title": title, "message": message}
    response = requests.post(
        HA_NOTIFY_ENDPOINT,
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def cleanup_files(paths) -> None:
    for p in paths:
        try:
            if p and p.exists():
                os.remove(p)
        except Exception as exc:
            log.warning("Could not remove %s: %s", p, exc)


def main() -> int:
    try:
        validate_config()
    except Exception as exc:
        log.error("Invalid configuration: %s", exc)
        return 1

    chunk_ranges = build_hour_chunks(HOURS_BACK)
    wav_paths = []
    merged_wav = None

    with tempfile.TemporaryDirectory(prefix="unifi_hist_") as temp_dir:
        tmp = Path(temp_dir)
        session = requests.Session()

        try:
            for i, (chunk_start, chunk_end) in enumerate(chunk_ranges, start=1):
                mp4_path = tmp / f"chunk_{i:03d}.mp4"
                wav_path = tmp / f"chunk_{i:03d}.wav"

                downloaded = download_chunk_mp4(session, chunk_start, chunk_end, mp4_path)
                if not downloaded:
                    cleanup_files([mp4_path, wav_path])
                    continue

                extracted = extract_wav_with_silence_removal(mp4_path, wav_path)

                # Critical for low-storage systems: remove large MP4 immediately.
                cleanup_files([mp4_path])

                if extracted:
                    wav_paths.append(wav_path)
                else:
                    cleanup_files([wav_path])

            if not wav_paths:
                send_home_assistant_notification(
                    message="Brak mowy do transkrypcji w zadanym zakresie czasu.",
                    title="UniFi Protect Transcription",
                )
                return 0

            merged_wav = tmp / "merged.wav"
            merged_ok = merge_wavs(wav_paths, merged_wav)
            if not merged_ok:
                send_home_assistant_notification(
                    message="Nie udało się połączyć plików audio.",
                    title="UniFi Protect Transcription",
                )
                return 1

            text = transcribe_with_openai(merged_wav)
            if not text:
                text = "(Brak treści po transkrypcji)"

            send_home_assistant_notification(
                message=text,
                title="UniFi Protect Transcription",
            )
            log.info("Notification sent to Home Assistant.")
            return 0

        except Exception as exc:
            log.exception("Processing failed: %s", exc)
            try:
                send_home_assistant_notification(
                    message=f"Transkrypcja nie powiodla sie: {exc}",
                    title="UniFi Protect Transcription",
                )
            except Exception:
                pass
            return 1

        finally:
            cleanup_files(wav_paths + ([merged_wav] if merged_wav else []))


if __name__ == "__main__":
    raise SystemExit(main())
