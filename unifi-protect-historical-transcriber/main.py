import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3


UNIFI_BASE_URL = os.environ.get("UNIFI_BASE_URL", "").rstrip("/")
UNIFI_API_KEY = os.environ.get("UNIFI_API_KEY", "")
UNIFI_USERNAME = os.environ.get("UNIFI_USERNAME", "")
UNIFI_PASSWORD = os.environ.get("UNIFI_PASSWORD", "")
CAMERA_ID = os.environ.get("CAMERA_ID", "")
HOURS_BACK = int(os.environ.get("HOURS_BACK", "6"))
WHISPER_ENABLED = os.environ.get("WHISPER_ENABLED", "true").lower() == "true"
WHISPER_API_URL = os.environ.get("WHISPER_API_URL", "https://api.openai.com/v1/audio/transcriptions")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "pl")
EXPORT_AUDIO_DIR = Path(os.environ.get("EXPORT_AUDIO_DIR", "/share/unifi_protect_audio"))
KEEP_AUDIO_FILES = os.environ.get("KEEP_AUDIO_FILES", "true").lower() == "true"
LOCAL_INPUT_DIR = os.environ.get("LOCAL_INPUT_DIR", "").strip()
SILENCE_THRESHOLD_DB = os.environ.get("SILENCE_THRESHOLD_DB", "-40dB")
START_SILENCE_DURATION = float(os.environ.get("START_SILENCE_DURATION", "0.2"))
STOP_SILENCE_DURATION = float(os.environ.get("STOP_SILENCE_DURATION", "0.5"))
VERIFY_TLS = os.environ.get("VERIFY_TLS", "false").lower() == "true"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

UNIFI_EXPORT_PATH = "/proxy/protect/api/video/export"
HA_NOTIFY_ENDPOINT = "http://supervisor/core/api/services/persistent_notification/create"
CHUNK_DURATION = timedelta(hours=1)

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("unifi-historical-transcriber")


class AuthError(Exception):
    pass


def use_api_key_auth() -> bool:
    return bool(UNIFI_API_KEY)


# ---------------------------------------------------------------------------
# UniFi Protect auth: cookie TOKEN from /api/auth/login
# Based on: github.com/danielfernau/unifi-protect-video-downloader
# ---------------------------------------------------------------------------

def unifi_login(session: requests.Session) -> str:
    url = f"{UNIFI_BASE_URL}/api/auth/login"
    payload = {"username": UNIFI_USERNAME, "password": UNIFI_PASSWORD}

    log.info("Authenticating to UniFi OS as '%s' ...", UNIFI_USERNAME)
    resp = session.post(url, json=payload, verify=VERIFY_TLS, timeout=15)

    if resp.status_code == 401:
        raise AuthError("UniFi login failed: invalid username or password.")
    resp.raise_for_status()

    token = resp.cookies.get("TOKEN")
    if not token:
        raise AuthError("Login succeeded but no TOKEN cookie received.")

    log.info("UniFi login OK (TOKEN cookie obtained).")
    return token


def authenticate_unifi(session: requests.Session) -> str:
    """Returns TOKEN cookie value (credential flow) or empty string (API key flow)."""
    if use_api_key_auth():
        log.info("Using X-API-Key authentication.")
        return ""

    return unifi_login(session)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def is_local_mode() -> bool:
    return bool(LOCAL_INPUT_DIR)


def validate_config() -> None:
    if not is_local_mode():
        if not UNIFI_BASE_URL:
            raise ValueError("UNIFI_BASE_URL is empty")
        if not CAMERA_ID:
            raise ValueError("CAMERA_ID is empty")
        if not UNIFI_API_KEY and not (UNIFI_USERNAME and UNIFI_PASSWORD):
            raise ValueError("Provide either unifi_api_key OR unifi_username + unifi_password")
        if HOURS_BACK <= 0:
            raise ValueError("HOURS_BACK must be > 0")
    else:
        input_dir = Path(LOCAL_INPUT_DIR)
        if not input_dir.is_dir():
            raise ValueError(f"local_input_dir does not exist: {LOCAL_INPUT_DIR}")
        mp4s = list(input_dir.glob("*.mp4"))
        if not mp4s:
            raise ValueError(f"No MP4 files found in {LOCAL_INPUT_DIR}")
    if WHISPER_ENABLED and not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is empty")


# ---------------------------------------------------------------------------
# Time chunking
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Video download
# ---------------------------------------------------------------------------

def download_chunk_mp4(
    session: requests.Session,
    token: str,
    chunk_start: datetime,
    chunk_end: datetime,
    output_path: Path,
) -> bool:
    url = f"{UNIFI_BASE_URL}{UNIFI_EXPORT_PATH}"
    params = {
        "camera": CAMERA_ID,
        "start": int(chunk_start.timestamp() * 1000),
        "end": int(chunk_end.timestamp() * 1000),
    }
    headers = {}
    cookies = {}

    if use_api_key_auth():
        headers["X-API-Key"] = UNIFI_API_KEY
    elif token:
        cookies["TOKEN"] = token

    auth_mode = "api_key" if use_api_key_auth() else "cookie_token"
    log.info(
        "Downloading chunk %s -> %s  (url=%s, camera=%s, auth=%s)",
        chunk_start.isoformat(),
        chunk_end.isoformat(),
        url,
        CAMERA_ID,
        auth_mode,
    )
    with session.get(
        url,
        headers=headers,
        cookies=cookies,
        params=params,
        stream=True,
        timeout=(10, 600),
        verify=VERIFY_TLS,
    ) as resp:
        if resp.status_code in (401, 403):
            if not use_api_key_auth() and token:
                log.warning("Token may have expired, re-authenticating...")
                try:
                    new_token = unifi_login(session)
                except AuthError:
                    raise
                retry_resp = session.get(
                    url,
                    cookies={"TOKEN": new_token},
                    params=params,
                    stream=True,
                    timeout=(10, 600),
                    verify=VERIFY_TLS,
                )
                if retry_resp.status_code < 400:
                    resp = retry_resp
                else:
                    body = retry_resp.text[:300]
                    log.error("Re-auth failed (HTTP %s): %s", retry_resp.status_code, body)
                    raise AuthError(f"HTTP {retry_resp.status_code}: {body}")
            else:
                body = resp.text[:300]
                method = "X-API-Key" if use_api_key_auth() else "cookie_token"
                log.error(
                    "UniFi API authentication failed (HTTP %s): %s\n"
                    "  Auth method: %s\n"
                    "  Hints:\n"
                    "  - If using API Key: verify it in UniFi OS -> Settings -> Advanced\n"
                    "  - If using credentials: check username/password, user needs Protect access\n"
                    "  - Aborting all remaining chunks.",
                    resp.status_code,
                    body,
                    method,
                )
                raise AuthError(f"HTTP {resp.status_code}: {body}")

        if resp.status_code >= 400:
            log.error("UniFi API error %s: %s", resp.status_code, resp.text[:300])
            return False

        with open(output_path, "wb") as f:
            for piece in resp.iter_content(chunk_size=1024 * 1024):
                if piece:
                    f.write(piece)

    size = output_path.stat().st_size if output_path.exists() else 0
    if size == 0:
        log.warning("Downloaded MP4 is empty for chunk %s", chunk_start.isoformat())
        return False

    log.info("Chunk downloaded: %s (%.1f MB)", output_path.name, size / 1024 / 1024)
    return True


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

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

    if len(wav_paths) == 1:
        shutil.copy2(wav_paths[0], merged_path)
        return merged_path.exists() and merged_path.stat().st_size > 44

    concat_list = merged_path.parent / "concat_list.txt"
    with open(concat_list, "w") as f:
        for p in wav_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        str(merged_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    cleanup_files([concat_list])

    if result.returncode != 0:
        log.error("ffmpeg merge failed: %s", result.stderr.strip())
        return False

    return merged_path.exists() and merged_path.stat().st_size > 44


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def transcribe_with_whisper_api(audio_path: Path) -> str:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = {"model": WHISPER_MODEL, "language": WHISPER_LANGUAGE}
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path.name, f, "audio/wav")}
        response = requests.post(
            WHISPER_API_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=300,
        )
    response.raise_for_status()
    return response.json().get("text", "").strip()


# ---------------------------------------------------------------------------
# HA notification
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def cleanup_files(paths) -> None:
    for p in paths:
        try:
            if p and p.exists():
                os.remove(p)
        except Exception as exc:
            log.warning("Could not remove %s: %s", p, exc)


def persist_audio_file(src: Path) -> Path:
    EXPORT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = EXPORT_AUDIO_DIR / f"unifi_protect_{CAMERA_ID}_{ts}.wav"
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect_wavs_from_download(tmp: Path, session: requests.Session) -> list[Path]:
    token = authenticate_unifi(session)
    chunk_ranges = build_hour_chunks(HOURS_BACK)
    wav_paths: list[Path] = []

    for i, (chunk_start, chunk_end) in enumerate(chunk_ranges, start=1):
        mp4_path = tmp / f"chunk_{i:03d}.mp4"
        wav_path = tmp / f"chunk_{i:03d}.wav"

        try:
            downloaded = download_chunk_mp4(
                session, token, chunk_start, chunk_end, mp4_path
            )
        except AuthError:
            send_home_assistant_notification(
                message="Autoryzacja UniFi Protect nie powiodla sie (401/403). Sprawdz dane logowania.",
                title="UniFi Protect Error",
            )
            raise

        if not downloaded:
            cleanup_files([mp4_path, wav_path])
            continue

        extracted = extract_wav_with_silence_removal(mp4_path, wav_path)
        cleanup_files([mp4_path])

        if extracted:
            wav_paths.append(wav_path)
        else:
            cleanup_files([wav_path])

    return wav_paths


def collect_wavs_from_local(tmp: Path) -> list[Path]:
    input_dir = Path(LOCAL_INPUT_DIR)
    mp4_files = sorted(input_dir.glob("*.mp4"))
    log.info("Local mode: found %d MP4 file(s) in %s", len(mp4_files), input_dir)

    wav_paths: list[Path] = []
    for i, mp4_path in enumerate(mp4_files, start=1):
        wav_path = tmp / f"local_{i:03d}.wav"
        extracted = extract_wav_with_silence_removal(mp4_path, wav_path)
        if extracted:
            wav_paths.append(wav_path)
        else:
            cleanup_files([wav_path])

    return wav_paths


def main() -> int:
    try:
        validate_config()
    except Exception as exc:
        log.error("Invalid configuration: %s", exc)
        return 1

    wav_paths: list[Path] = []
    merged_wav = None
    exported_audio = None

    with tempfile.TemporaryDirectory(prefix="unifi_hist_") as temp_dir:
        tmp = Path(temp_dir)

        try:
            if is_local_mode():
                log.info("Running in LOCAL mode (processing existing MP4 files).")
                wav_paths = collect_wavs_from_local(tmp)
            else:
                log.info("Running in DOWNLOAD mode (fetching from UniFi Protect).")
                session = requests.Session()
                wav_paths = collect_wavs_from_download(tmp, session)

            if not wav_paths:
                send_home_assistant_notification(
                    message="Brak mowy do transkrypcji/eksportu w zadanym zakresie czasu.",
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

            if KEEP_AUDIO_FILES or not WHISPER_ENABLED:
                exported_audio = persist_audio_file(merged_wav)
                log.info("Exported processed audio to %s", exported_audio)

            if WHISPER_ENABLED:
                text = transcribe_with_whisper_api(merged_wav)
                if not text:
                    text = "(Brak treści po transkrypcji)"

                send_home_assistant_notification(
                    message=text,
                    title="UniFi Protect Transcription",
                )
                log.info("Notification sent to Home Assistant.")
            else:
                msg = "Whisper disabled. Audio prepared."
                if exported_audio:
                    msg = f"{msg} File: {exported_audio}"
                send_home_assistant_notification(
                    message=msg,
                    title="UniFi Protect Audio Export",
                )
            return 0

        except AuthError as exc:
            log.error("Authentication failed: %s", exc)
            try:
                send_home_assistant_notification(
                    message=f"Autoryzacja UniFi nie powiodla sie: {exc}",
                    title="UniFi Protect Error",
                )
            except Exception:
                pass
            return 1

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
