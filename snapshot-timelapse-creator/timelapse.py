import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

FILENAME_PATTERNS = [
    re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})"),   # YYYYMMDD_HHMM
    re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})"), # YYYY-MM-DD_HH-MM
    re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})"),     # YYYYMMDDHHMM
    re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})"),  # YYYY-MM-DD_HHMM
]


def _parse_datetime_from_filename(name: str) -> Optional[datetime]:
    for pattern in FILENAME_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                return datetime(y, mo, d, h, mi)
            except (ValueError, IndexError):
                continue
    return None


def _get_file_datetime(path: Path) -> Optional[datetime]:
    dt = _parse_datetime_from_filename(path.name)
    if dt:
        return dt
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def scan_months(base_dir: Path) -> List[str]:
    if not base_dir.exists():
        log.warning("Snapshot dir does not exist: %s", base_dir)
        return []
    months = []
    for entry in os.scandir(base_dir):
        if entry.is_dir() and re.match(r"\d{4}-\d{2}$", entry.name):
            months.append(entry.name)
    result = sorted(months)
    log.info("Found %d month folders in %s: %s", len(result), base_dir, result)
    return result


def scan_snapshots(
    base_dir: Path,
    file_pattern: str,
    date_from: str,
    date_to: str,
    hour_from: int = 0,
    hour_to: int = 24,
) -> List[Path]:
    from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
    to_date = datetime.strptime(date_to, "%Y-%m-%d").date()

    from_month = from_date.strftime("%Y-%m")
    to_month = to_date.strftime("%Y-%m")
    relevant_months = [m for m in scan_months(base_dir) if from_month <= m <= to_month]
    log.info(
        "Scanning: %s -> %s (months: %s), hours %d-%d, pattern: %s",
        date_from, date_to, relevant_months, hour_from, hour_to, file_pattern,
    )

    snapshots: List[Tuple[float, Path]] = []
    total_files = 0
    skipped_no_date = 0
    skipped_date_range = 0
    skipped_hour_range = 0

    for month in relevant_months:
        month_dir = base_dir / month
        for f in month_dir.glob(file_pattern):
            if not f.is_file():
                continue
            total_files += 1

            dt = _get_file_datetime(f)
            if dt is None:
                skipped_no_date += 1
                continue

            if not (from_date <= dt.date() <= to_date):
                skipped_date_range += 1
                continue

            if not (hour_from <= dt.hour < hour_to):
                skipped_hour_range += 1
                continue

            snapshots.append((dt.timestamp(), f))

    log.info(
        "Scan result: %d files found, %d matched, %d skipped (no_date=%d, date_range=%d, hour=%d)",
        total_files, len(snapshots), skipped_no_date + skipped_date_range + skipped_hour_range,
        skipped_no_date, skipped_date_range, skipped_hour_range,
    )

    snapshots.sort(key=lambda x: x[0])
    return [p for _, p in snapshots]


def count_snapshots(
    base_dir: Path,
    file_pattern: str,
    date_from: str,
    date_to: str,
    hour_from: int = 0,
    hour_to: int = 24,
) -> int:
    return len(scan_snapshots(base_dir, file_pattern, date_from, date_to, hour_from, hour_to))


CACHE_PATH = Path("/data/.validation_cache.json")
_PARALLEL_WORKERS = 4


def _cache_key(path: Path) -> str:
    try:
        st = path.stat()
        return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return path.name


def _load_cache() -> Dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: Dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        log.warning("Failed to save validation cache")


def check_image(path: Path) -> Dict:
    """Single ffmpeg call: validate + brightness + saturation.

    Produces an 8x8 RGB thumbnail.  If ffmpeg succeeds the file is valid.
    Brightness and saturation are computed from the 192 output bytes.
    """
    try:
        if path.stat().st_size < 1000:
            return {"valid": False, "brightness": 0, "saturation": 0.0}

        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-vf", "scale=8:8",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0 or len(result.stdout) < 3:
            return {"valid": False, "brightness": 0, "saturation": 0.0}

        pixels = result.stdout
        total_brightness = 0
        total_saturation = 0.0
        count = 0
        for i in range(0, len(pixels) - 2, 3):
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            total_brightness += (r + g + b) // 3
            total_saturation += max(r, g, b) - min(r, g, b)
            count += 1

        return {
            "valid": True,
            "brightness": total_brightness // count if count else 0,
            "saturation": round(total_saturation / count, 1) if count else 0.0,
        }
    except Exception:
        return {"valid": False, "brightness": 0, "saturation": 0.0}


def generate_thumbnail(src: Path, dst: Path, size: int = 200) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-i", str(src),
                "-vf", f"scale={size}:-2",
                "-q:v", "8",
                str(dst),
            ],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


RESOLUTION_MAP = {
    "720p": 1280,
    "1080p": 1920,
    "480p": 854,
    "original": -1,
}


class TimelapseJob:
    def __init__(self, job_id: Optional[str] = None):
        self.id = job_id or str(uuid.uuid4())[:8]
        self.status = "pending"
        self.progress = 0
        self.total_frames = 0
        self.processed_frames = 0
        self.skipped_corrupt = 0
        self.skipped_dark = 0
        self.skipped_nightmode = 0
        self.skipped_sampling = 0
        self.used_frames = 0
        self.message = ""
        self.output_file: Optional[str] = None
        self.error: Optional[str] = None
        self._cancelled = False
        self._process: Optional[subprocess.Popen] = None

    def cancel(self):
        self._cancelled = True
        if self._process:
            try:
                self._process.kill()
            except Exception:
                pass

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "total_frames": self.total_frames,
            "processed_frames": self.processed_frames,
            "skipped_corrupt": self.skipped_corrupt,
            "skipped_dark": self.skipped_dark,
            "skipped_nightmode": self.skipped_nightmode,
            "skipped_sampling": self.skipped_sampling,
            "used_frames": self.used_frames,
            "message": self.message,
            "output_file": self.output_file,
            "error": self.error,
        }


def _build_ffmpeg_cmd(
    concat_path: str,
    output_path: str,
    width: int,
    max_threads: int,
    timestamp_overlay: bool = False,
) -> List[str]:
    vf_parts = []
    if width > 0:
        vf_parts.append(f"scale={width}:-2")

    vf = ",".join(vf_parts) if vf_parts else None

    cmd = [
        "nice", "-n", "19",
        "ffmpeg", "-y",
        "-threads", str(max_threads),
        "-f", "concat", "-safe", "0",
        "-i", concat_path,
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-v", "error",
        output_path,
    ]
    return cmd


def generate_timelapse(
    images: List[Path],
    output_path: Path,
    job: TimelapseJob,
    fps: int = 24,
    resolution: str = "720p",
    max_threads: int = 2,
    skip_dark: bool = False,
    brightness_threshold: int = 30,
    skip_night: bool = False,
    nightmode_threshold: int = 15,
    target_frames: int = 0,
) -> bool:
    width = RESOLUTION_MAP.get(resolution, 1280)
    needs_filtering = skip_dark or skip_night

    # Pre-filter sampling: check more frames than we need to account for
    # rejection by dark/night filters.  If no filters are active we can
    # sample exactly.  With filters we check ~4x target so that even a
    # 75 % rejection rate still yields enough frames.
    pre_sample = 1
    if target_frames > 0:
        pool = target_frames * 4 if needs_filtering else target_frames
        if len(images) > pool:
            pre_sample = max(1, len(images) // pool)

    job.status = "validating"
    job.total_frames = len(images)
    job.message = "Walidacja i filtrowanie klatek..."

    # Build list of candidates after pre-sampling
    candidates: List[Tuple[int, Path]] = []
    for i, img in enumerate(images):
        if pre_sample > 1 and (i % pre_sample) != 0:
            job.skipped_sampling += 1
            continue
        candidates.append((i, img))

    # Load cache, split into cached vs to-check
    cache = _load_cache()
    check_results: Dict[int, Dict] = {}
    to_check: List[Tuple[int, Path]] = []
    for i, img in candidates:
        key = _cache_key(img)
        if key in cache:
            check_results[i] = cache[key]
        else:
            to_check.append((i, img))

    job.message = f"Walidacja {len(to_check)} klatek ({len(check_results)} z cache)..."
    log.info(
        "Validation: %d candidates, %d cached, %d to check",
        len(candidates), len(check_results), len(to_check),
    )

    # Parallel validation for uncached images
    if to_check:
        done_count = len(check_results)
        total_to_process = len(candidates)
        with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as executor:
            futures = {
                executor.submit(check_image, img): (i, img)
                for i, img in to_check
            }
            for future in as_completed(futures):
                if job.is_cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    job.status = "cancelled"
                    job.message = "Anulowane"
                    return False

                idx, img = futures[future]
                result = future.result()
                check_results[idx] = result
                cache[_cache_key(img)] = result

                done_count += 1
                job.processed_frames = done_count
                job.progress = int(done_count / total_to_process * 50)

        _save_cache(cache)
    else:
        job.processed_frames = len(candidates)
        job.progress = 50

    # Apply filters on check results (in original order)
    valid_images: List[Path] = []
    for i, img in candidates:
        r = check_results.get(i)
        if not r or not r["valid"]:
            job.skipped_corrupt += 1
        elif skip_dark and r["brightness"] < brightness_threshold:
            job.skipped_dark += 1
        elif skip_night and r["saturation"] < nightmode_threshold:
            job.skipped_nightmode += 1
        else:
            valid_images.append(img)

    if not valid_images:
        job.status = "error"
        job.error = "Brak prawidlowych klatek po filtrowaniu"
        return False

    # Final sampling: trim valid frames to exact target
    if target_frames > 0 and len(valid_images) > target_frames:
        trimmed = len(valid_images) - target_frames
        step = len(valid_images) / target_frames
        valid_images = [valid_images[int(i * step)] for i in range(target_frames)]
        job.skipped_sampling += trimmed

    job.used_frames = len(valid_images)
    job.message = f"Generowanie timelapse z {len(valid_images)} klatek..."
    job.status = "generating"

    concat_fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="tl_concat_"
    )
    try:
        duration = 1.0 / fps
        for img in valid_images:
            concat_fd.write(f"file '{img}'\n")
            concat_fd.write(f"duration {duration}\n")
        concat_fd.write(f"file '{valid_images[-1]}'\n")
        concat_fd.close()

        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = _build_ffmpeg_cmd(
            concat_fd.name, str(output_path), width, max_threads
        )
        log.info("ffmpeg cmd: %s", " ".join(cmd))

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        job._process = process

        total = len(valid_images)
        for raw_line in iter(process.stdout.readline, b""):
            if job.is_cancelled:
                process.kill()
                job.status = "cancelled"
                job.message = "Anulowane"
                return False

            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("frame="):
                try:
                    frame = int(line.split("=", 1)[1].strip())
                    job.progress = 50 + int(frame / total * 50)
                    job.message = f"Enkodowanie klatki {frame}/{total}..."
                except ValueError:
                    pass
            elif line == "progress=end":
                break

        process.wait(timeout=30)
        stderr_out = process.stderr.read().decode("utf-8", errors="replace")

        if process.returncode == 0:
            job.status = "done"
            job.progress = 100
            job.output_file = output_path.name
            size_mb = output_path.stat().st_size / (1024 * 1024)
            job.message = f"Gotowe! {len(valid_images)} klatek -> {output_path.name} ({size_mb:.1f} MB)"
            return True

        job.status = "error"
        job.error = f"ffmpeg error (code {process.returncode}): {stderr_out[-500:]}"
        return False

    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        return False
    finally:
        try:
            os.unlink(concat_fd.name)
        except OSError:
            pass


def generate_preview(
    images: List[Path],
    output_path: Path,
    job: TimelapseJob,
    fps: int = 24,
    max_threads: int = 2,
    skip_dark: bool = False,
    brightness_threshold: int = 30,
    skip_night: bool = False,
    nightmode_threshold: int = 15,
    max_frames: int = 200,
) -> bool:
    return generate_timelapse(
        images=images,
        output_path=output_path,
        job=job,
        fps=fps,
        resolution="480p",
        max_threads=max_threads,
        skip_dark=skip_dark,
        brightness_threshold=brightness_threshold,
        skip_night=skip_night,
        nightmode_threshold=nightmode_threshold,
        target_frames=max_frames,
    )


def get_sample_snapshots(snapshots: List[Path], count: int = 8) -> List[Path]:
    if len(snapshots) <= count:
        return list(snapshots)
    step = len(snapshots) / count
    return [snapshots[int(i * step)] for i in range(count)]
