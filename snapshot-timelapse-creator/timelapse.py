import logging
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def scan_months(base_dir: Path) -> List[str]:
    if not base_dir.exists():
        return []
    months = []
    for entry in os.scandir(base_dir):
        if entry.is_dir() and re.match(r"\d{4}-\d{2}$", entry.name):
            months.append(entry.name)
    return sorted(months)


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

    snapshots = []
    for month in relevant_months:
        month_dir = base_dir / month
        for f in month_dir.glob(file_pattern):
            if not f.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if from_date <= mtime.date() <= to_date and hour_from <= mtime.hour < hour_to:
                snapshots.append((mtime.timestamp(), f))

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


def validate_image(path: Path) -> bool:
    try:
        if path.stat().st_size < 1000:
            return False
        result = subprocess.run(
            ["ffprobe", "-v", "error", str(path)],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_brightness(path: Path) -> int:
    """Average brightness 0-255 via 1x1 grayscale downscale."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-vf", "scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "gray", "-",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and len(result.stdout) >= 1:
            return result.stdout[0]
        return 128
    except Exception:
        return 128


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
    skip_every: int = 1,
) -> bool:
    width = RESOLUTION_MAP.get(resolution, 1280)

    job.status = "validating"
    job.total_frames = len(images)
    job.message = "Walidacja i filtrowanie klatek..."

    valid_images: List[Path] = []

    for i, img in enumerate(images):
        if job.is_cancelled:
            job.status = "cancelled"
            job.message = "Anulowane"
            return False

        job.processed_frames = i + 1
        job.progress = int((i + 1) / len(images) * 50)

        if skip_every > 1 and (i % skip_every) != 0:
            continue

        if not validate_image(img):
            job.skipped_corrupt += 1
            continue

        if skip_dark:
            brightness = get_brightness(img)
            if brightness < brightness_threshold:
                job.skipped_dark += 1
                continue

        valid_images.append(img)

    if not valid_images:
        job.status = "error"
        job.error = "Brak prawidlowych klatek po filtrowaniu"
        return False

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
    max_frames: int = 200,
) -> bool:
    skip_every = max(1, len(images) // max_frames)
    return generate_timelapse(
        images=images,
        output_path=output_path,
        job=job,
        fps=fps,
        resolution="480p",
        max_threads=max_threads,
        skip_dark=skip_dark,
        brightness_threshold=brightness_threshold,
        skip_every=skip_every,
    )


def get_sample_snapshots(snapshots: List[Path], count: int = 8) -> List[Path]:
    if len(snapshots) <= count:
        return list(snapshots)
    step = len(snapshots) / count
    return [snapshots[int(i * step)] for i in range(count)]
