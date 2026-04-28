"""Storage management for snapshot files.

Provides delete / archive / compress operations on the snapshot directory
with progress reporting via a CleanupJob (mirrors TimelapseJob style).
"""

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from timelapse import scan_months, scan_snapshots

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage overview
# ---------------------------------------------------------------------------

def get_storage_overview(base_dir: Path, file_pattern: str) -> Dict:
    """Return per-month statistics: file count + total bytes."""
    months = []
    grand_total_files = 0
    grand_total_bytes = 0

    if not base_dir.exists():
        return {"months": [], "total_files": 0, "total_bytes": 0, "exists": False}

    for month in scan_months(base_dir):
        month_dir = base_dir / month
        count = 0
        total_bytes = 0
        for f in month_dir.glob(file_pattern):
            if f.is_file():
                try:
                    total_bytes += f.stat().st_size
                    count += 1
                except OSError:
                    pass
        months.append({
            "month": month,
            "count": count,
            "size_bytes": total_bytes,
        })
        grand_total_files += count
        grand_total_bytes += total_bytes

    # Add disk free info for the snapshot dir
    free_bytes = 0
    total_disk_bytes = 0
    try:
        usage = shutil.disk_usage(base_dir)
        free_bytes = usage.free
        total_disk_bytes = usage.total
    except OSError:
        pass

    return {
        "months": months,
        "total_files": grand_total_files,
        "total_bytes": grand_total_bytes,
        "free_bytes": free_bytes,
        "total_disk_bytes": total_disk_bytes,
        "exists": True,
    }


def preview_cleanup(
    base_dir: Path,
    file_pattern: str,
    date_from: str,
    date_to: str,
    hour_from: int = 0,
    hour_to: int = 24,
) -> Dict:
    """Return how many files / bytes would be affected by a cleanup."""
    files = scan_snapshots(base_dir, file_pattern, date_from, date_to, hour_from, hour_to)
    total_bytes = 0
    for f in files:
        try:
            total_bytes += f.stat().st_size
        except OSError:
            pass
    return {"count": len(files), "size_bytes": total_bytes}


# ---------------------------------------------------------------------------
# Cleanup job
# ---------------------------------------------------------------------------

class CleanupJob:
    def __init__(self, action: str, job_id: Optional[str] = None):
        self.id = job_id or str(uuid.uuid4())[:8]
        self.action = action  # "delete" | "archive" | "compress"
        self.status = "pending"
        self.progress = 0
        self.total_files = 0
        self.processed_files = 0
        self.failed_files = 0
        self.bytes_before = 0
        self.bytes_after = 0
        self.message = ""
        self.archive_file: Optional[str] = None
        self.error: Optional[str] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def to_dict(self) -> dict:
        bytes_freed = max(0, self.bytes_before - self.bytes_after)
        return {
            "id": self.id,
            "action": self.action,
            "status": self.status,
            "progress": self.progress,
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "failed_files": self.failed_files,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "bytes_freed": bytes_freed,
            "message": self.message,
            "archive_file": self.archive_file,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def cleanup_delete(files: List[Path], job: CleanupJob) -> bool:
    job.status = "running"
    job.total_files = len(files)
    job.message = f"Usuwanie {len(files)} plikow..."

    for i, f in enumerate(files):
        if job.is_cancelled:
            job.status = "cancelled"
            job.message = "Anulowane"
            return False
        size = _file_size(f)
        job.bytes_before += size
        try:
            f.unlink()
            job.processed_files += 1
        except OSError as exc:
            log.warning("Failed to delete %s: %s", f, exc)
            job.failed_files += 1
            job.bytes_after += size

        job.progress = int((i + 1) / len(files) * 100) if files else 100

    job.status = "done"
    job.message = (
        f"Usunieto {job.processed_files} plikow, "
        f"zwolniono {(job.bytes_before - job.bytes_after) / 1024 / 1024:.1f} MB"
    )
    return True


def cleanup_archive(
    files: List[Path],
    archive_dir: Path,
    archive_name: str,
    base_dir: Path,
    job: CleanupJob,
    delete_after: bool = True,
) -> bool:
    job.status = "running"
    job.total_files = len(files)
    job.message = f"Archiwizacja {len(files)} plikow..."

    archive_dir.mkdir(parents=True, exist_ok=True)
    safe_name = archive_name if archive_name.endswith(".zip") else archive_name + ".zip"
    archive_path = archive_dir / safe_name

    if archive_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"{archive_path.stem}_{ts}.zip"

    try:
        # ZIP_STORED: no recompression - jpegs are already compressed.
        # Also more reliable for large archives (no zlib pressure).
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for i, f in enumerate(files):
                if job.is_cancelled:
                    break

                size = _file_size(f)
                job.bytes_before += size
                try:
                    arcname = f.relative_to(base_dir)
                except ValueError:
                    arcname = Path(f.parent.name) / f.name

                try:
                    zf.write(f, arcname=str(arcname))
                    job.processed_files += 1
                except (OSError, zipfile.BadZipFile) as exc:
                    log.warning("Failed to archive %s: %s", f, exc)
                    job.failed_files += 1

                # Halve the progress range when also deleting afterwards
                if delete_after:
                    job.progress = int((i + 1) / len(files) * 50) if files else 50
                else:
                    job.progress = int((i + 1) / len(files) * 100) if files else 100

        if job.is_cancelled:
            try:
                archive_path.unlink()
            except OSError:
                pass
            job.status = "cancelled"
            job.message = "Anulowane"
            return False

        job.archive_file = archive_path.name
        archive_size = _file_size(archive_path)

        if delete_after:
            job.message = f"Archiwum gotowe ({archive_size / 1024 / 1024:.1f} MB), usuwanie oryginalow..."
            for i, f in enumerate(files):
                if job.is_cancelled:
                    job.status = "cancelled"
                    job.message = "Anulowane (oryginaly nie usuniete)"
                    return False
                try:
                    if f.exists():
                        f.unlink()
                except OSError as exc:
                    log.warning("Failed to delete %s after archiving: %s", f, exc)
                job.progress = 50 + int((i + 1) / len(files) * 50) if files else 100

            # bytes_after = size of archive only (originals deleted)
            job.bytes_after = archive_size
        else:
            # bytes_after = original size + archive overhead
            job.bytes_after = job.bytes_before + archive_size

        job.status = "done"
        freed = max(0, job.bytes_before - job.bytes_after)
        job.message = (
            f"Zarchiwizowano {job.processed_files} plikow do {archive_path.name} "
            f"({archive_size / 1024 / 1024:.1f} MB, zwolniono {freed / 1024 / 1024:.1f} MB)"
        )
        return True

    except Exception as exc:
        log.exception("Archive failed")
        job.status = "error"
        job.error = str(exc)
        try:
            if archive_path.exists():
                archive_path.unlink()
        except OSError:
            pass
        return False


def cleanup_compress(
    files: List[Path],
    job: CleanupJob,
    quality: int = 70,
    max_width: int = 0,
) -> bool:
    """Re-encode JPGs in place with lower quality and optional downscale.

    Uses ffmpeg to write a temp file then atomically replaces the original.
    Files that grow after compression are kept untouched (we never make
    things worse).
    """
    job.status = "running"
    job.total_files = len(files)
    job.message = f"Kompresja {len(files)} plikow (q={quality}, maxw={max_width or 'oryg'})..."

    quality = max(2, min(31, int(round(31 - (quality / 100) * 29))))
    # ffmpeg -q:v: 2 (best) .. 31 (worst). Map UI quality (0-100) -> ffmpeg q.

    for i, f in enumerate(files):
        if job.is_cancelled:
            job.status = "cancelled"
            job.message = "Anulowane"
            return False

        original_size = _file_size(f)
        job.bytes_before += original_size

        if not f.is_file():
            job.failed_files += 1
            job.bytes_after += original_size
            continue

        tmp_fd = tempfile.NamedTemporaryFile(
            suffix=f.suffix, delete=False, prefix="tl_compress_", dir=str(f.parent)
        )
        tmp_path = Path(tmp_fd.name)
        tmp_fd.close()

        try:
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(f)]
            vf_parts = []
            if max_width > 0:
                vf_parts.append(f"scale='min({max_width},iw)':-2")
            if vf_parts:
                cmd += ["-vf", ",".join(vf_parts)]
            cmd += ["-q:v", str(quality), str(tmp_path)]

            result = subprocess.run(cmd, capture_output=True, timeout=60)

            if result.returncode != 0 or not tmp_path.exists():
                log.warning("ffmpeg failed for %s: %s", f, result.stderr[-300:])
                job.failed_files += 1
                job.bytes_after += original_size
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                continue

            new_size = _file_size(tmp_path)
            # Only replace if we actually saved space
            if new_size > 0 and new_size < original_size:
                try:
                    # Preserve mtime so date detection still works
                    st = f.stat()
                    os.replace(str(tmp_path), str(f))
                    os.utime(str(f), (st.st_atime, st.st_mtime))
                    job.bytes_after += new_size
                    job.processed_files += 1
                except OSError as exc:
                    log.warning("Failed to replace %s: %s", f, exc)
                    job.failed_files += 1
                    job.bytes_after += original_size
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
            else:
                # No saving - keep original
                job.bytes_after += original_size
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        except subprocess.TimeoutExpired:
            log.warning("ffmpeg timeout for %s", f)
            job.failed_files += 1
            job.bytes_after += original_size
            try:
                tmp_path.unlink()
            except OSError:
                pass
        except Exception as exc:
            log.warning("Compression error for %s: %s", f, exc)
            job.failed_files += 1
            job.bytes_after += original_size
            try:
                tmp_path.unlink()
            except OSError:
                pass

        job.progress = int((i + 1) / len(files) * 100) if files else 100

    job.status = "done"
    freed = max(0, job.bytes_before - job.bytes_after)
    job.message = (
        f"Skompresowano {job.processed_files} plikow, "
        f"zwolniono {freed / 1024 / 1024:.1f} MB "
        f"(blednych: {job.failed_files})"
    )
    return True


# ---------------------------------------------------------------------------
# Archive listing (for the outputs/archives panel)
# ---------------------------------------------------------------------------

def list_archives(archive_dir: Path) -> List[Dict]:
    if not archive_dir.exists():
        return []
    items = []
    for p in sorted(archive_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            items.append({
                "name": p.name,
                "size_mb": f"{st.st_size / 1024 / 1024:.1f}",
                "size_bytes": st.st_size,
                "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        except OSError:
            continue
    return items
