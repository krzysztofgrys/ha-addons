import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

from storage import (
    CleanupJob,
    cleanup_archive,
    cleanup_compress,
    cleanup_delete,
    get_storage_overview,
    list_archives,
    preview_cleanup,
)
from timelapse import (
    RESOLUTION_MAP,
    TimelapseJob,
    _get_file_datetime,
    count_snapshots,
    generate_preview,
    generate_thumbnail,
    generate_timelapse,
    get_sample_snapshots,
    scan_months,
    scan_snapshots,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/homeassistant/timelapse"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/share/timelapses"))
ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/share/timelapse_archives"))
FILE_PATTERN = os.environ.get("FILE_PATTERN", "*.jpg")
MAX_THREADS = int(os.environ.get("MAX_THREADS", "2"))
BRIGHTNESS_THRESHOLD = int(os.environ.get("BRIGHTNESS_THRESHOLD", "30"))
NIGHTMODE_THRESHOLD = int(os.environ.get("NIGHTMODE_THRESHOLD", "15"))
THUMB_DIR = Path("/data/.thumbs")

app = Flask(__name__)

job_lock = threading.Lock()
current_job: TimelapseJob | None = None

cleanup_lock = threading.Lock()
current_cleanup: CleanupJob | None = None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        months=scan_months(SNAPSHOT_DIR),
        resolutions=list(RESOLUTION_MAP.keys()),
        brightness_threshold=BRIGHTNESS_THRESHOLD,
        nightmode_threshold=NIGHTMODE_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# API: scanning
# ---------------------------------------------------------------------------

@app.route("/api/months")
def api_months():
    return jsonify({"months": scan_months(SNAPSHOT_DIR)})


@app.route("/api/debug")
def api_debug():
    """Diagnostic endpoint for troubleshooting path/scanning issues."""
    exists = SNAPSHOT_DIR.exists()
    months = scan_months(SNAPSHOT_DIR) if exists else []
    sample_files = []
    if months:
        first_month = SNAPSHOT_DIR / months[0]
        for i, f in enumerate(first_month.glob(FILE_PATTERN)):
            if i >= 5:
                break
            dt = _get_file_datetime(f)
            sample_files.append({
                "name": f.name,
                "parsed_datetime": dt.strftime("%Y-%m-%d %H:%M") if dt else None,
                "size_bytes": f.stat().st_size,
            })
    return jsonify({
        "snapshot_dir": str(SNAPSHOT_DIR),
        "snapshot_dir_exists": exists,
        "file_pattern": FILE_PATTERN,
        "months_found": months,
        "sample_files_from_first_month": sample_files,
    })


@app.route("/api/stats")
def api_stats():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    hour_from = int(request.args.get("hour_from", 0))
    hour_to = int(request.args.get("hour_to", 24))

    if not date_from or not date_to:
        return jsonify({"error": "from and to are required"}), 400

    total = count_snapshots(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    return jsonify({
        "total": total,
        "date_from": date_from,
        "date_to": date_to,
        "hour_from": hour_from,
        "hour_to": hour_to,
    })


@app.route("/api/samples")
def api_samples():
    """Return paths to sample snapshots for thumbnail preview strip."""
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    hour_from = int(request.args.get("hour_from", 0))
    hour_to = int(request.args.get("hour_to", 24))

    if not date_from or not date_to:
        return jsonify({"error": "from and to are required"}), 400

    all_snaps = scan_snapshots(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    samples = get_sample_snapshots(all_snaps, 8)

    items = []
    for s in samples:
        dt = _get_file_datetime(s)
        if dt is None:
            dt = datetime.fromtimestamp(s.stat().st_mtime)
        month = dt.strftime("%Y-%m")
        items.append({
            "month": month,
            "filename": s.name,
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify({"samples": items, "total": len(all_snaps)})


# ---------------------------------------------------------------------------
# API: thumbnails
# ---------------------------------------------------------------------------

@app.route("/api/thumbnail/<month>/<filename>")
def api_thumbnail(month, filename):
    src = SNAPSHOT_DIR / month / filename
    if not src.exists():
        return jsonify({"error": "Not found"}), 404

    thumb_path = THUMB_DIR / month / filename
    if not thumb_path.exists():
        if not generate_thumbnail(src, thumb_path):
            return send_file(src)

    return send_file(thumb_path, mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# API: timelapse generation
# ---------------------------------------------------------------------------

@app.route("/api/generate", methods=["POST"])
def api_generate():
    global current_job
    with job_lock:
        if current_job and current_job.status in ("validating", "generating"):
            return jsonify({"error": "Job already running"}), 409
    with cleanup_lock:
        if current_cleanup and current_cleanup.status in ("pending", "running"):
            return jsonify({
                "error": "Trwa sprzatanie pamieci - poczekaj az sie zakonczy"
            }), 409

    data = request.get_json(force=True)
    date_from = data.get("from", "")
    date_to = data.get("to", "")
    hour_from = int(data.get("hour_from", 0))
    hour_to = int(data.get("hour_to", 24))
    fps = int(data.get("fps", 24))
    resolution = data.get("resolution", "720p")
    target_duration = int(data.get("target_duration", 0))
    skip_dark = bool(data.get("skip_dark", False))
    skip_night = bool(data.get("skip_night", False))
    is_preview = bool(data.get("preview", False))

    if not date_from or not date_to:
        return jsonify({"error": "from and to required"}), 400

    images = scan_snapshots(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    if not images:
        return jsonify({"error": "No snapshots found for this range"}), 404

    job = TimelapseJob()
    job.source_range = {
        "from": date_from,
        "to": date_to,
        "hour_from": hour_from,
        "hour_to": hour_to,
        "preview": is_preview,
    }
    with job_lock:
        current_job = job

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_preview" if is_preview else ""
    out_name = f"timelapse_{date_from}_{date_to}{suffix}_{ts}.mp4"
    out_path = OUTPUT_DIR / out_name

    def _run():
        try:
            if is_preview:
                generate_preview(
                    images, out_path, job,
                    fps=fps, max_threads=MAX_THREADS,
                    skip_dark=skip_dark, brightness_threshold=BRIGHTNESS_THRESHOLD,
                    skip_night=skip_night, nightmode_threshold=NIGHTMODE_THRESHOLD,
                )
            else:
                target_frames = target_duration * fps if target_duration > 0 else 0

                generate_timelapse(
                    images, out_path, job,
                    fps=fps, resolution=resolution, max_threads=MAX_THREADS,
                    skip_dark=skip_dark, brightness_threshold=BRIGHTNESS_THRESHOLD,
                    skip_night=skip_night, nightmode_threshold=NIGHTMODE_THRESHOLD,
                    target_frames=target_frames,
                )
        except Exception as exc:
            log.exception("Job failed")
            job.status = "error"
            job.error = str(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/job")
def api_job_status():
    with job_lock:
        if current_job is None:
            return jsonify({"job": None})
        data = current_job.to_dict()
        data["source_range"] = getattr(current_job, "source_range", None)
        return jsonify({"job": data})


@app.route("/api/job/cancel", methods=["POST"])
def api_job_cancel():
    with job_lock:
        if current_job and current_job.status in ("validating", "generating"):
            current_job.cancel()
            return jsonify({"ok": True})
    return jsonify({"error": "No running job"}), 404


# ---------------------------------------------------------------------------
# API: outputs
# ---------------------------------------------------------------------------

@app.route("/api/outputs")
def api_outputs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        files.append({
            "name": p.name,
            "size_mb": f"{st.st_size / 1024 / 1024:.1f}",
            "created": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        })
    return jsonify({"files": files})


@app.route("/api/outputs/<filename>")
def api_download(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True)


@app.route("/api/outputs/<filename>/stream")
def api_stream(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, mimetype="video/mp4")


@app.route("/api/outputs/<filename>", methods=["DELETE"])
def api_delete(filename):
    path = OUTPUT_DIR / filename
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: storage management
# ---------------------------------------------------------------------------

@app.route("/api/storage")
def api_storage():
    overview = get_storage_overview(SNAPSHOT_DIR, FILE_PATTERN)
    overview["snapshot_dir"] = str(SNAPSHOT_DIR)
    overview["archive_dir"] = str(ARCHIVE_DIR)
    overview["archives"] = list_archives(ARCHIVE_DIR)
    return jsonify(overview)


@app.route("/api/storage/preview")
def api_storage_preview():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    hour_from = int(request.args.get("hour_from", 0))
    hour_to = int(request.args.get("hour_to", 24))

    if not date_from or not date_to:
        return jsonify({"error": "from and to are required"}), 400

    info = preview_cleanup(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    return jsonify(info)


@app.route("/api/storage/cleanup", methods=["POST"])
def api_storage_cleanup():
    global current_cleanup

    with cleanup_lock:
        if current_cleanup and current_cleanup.status in ("pending", "running"):
            return jsonify({"error": "Cleanup already running"}), 409

    with job_lock:
        if current_job and current_job.status in ("validating", "generating"):
            return jsonify({
                "error": "Nie mozna sprzatac w trakcie generowania timelapse"
            }), 409

    data = request.get_json(force=True) or {}
    action = data.get("action", "")
    if action not in ("delete", "archive", "compress"):
        return jsonify({"error": "Invalid action (delete|archive|compress)"}), 400

    date_from = data.get("from", "")
    date_to = data.get("to", "")
    hour_from = int(data.get("hour_from", 0))
    hour_to = int(data.get("hour_to", 24))
    if not date_from or not date_to:
        return jsonify({"error": "from and to required"}), 400

    files = scan_snapshots(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    if not files:
        return jsonify({"error": "Brak plikow do przetworzenia w tym zakresie"}), 404

    job = CleanupJob(action=action)
    with cleanup_lock:
        current_cleanup = job

    def _run():
        try:
            if action == "delete":
                cleanup_delete(files, job)
            elif action == "archive":
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                name = f"snapshots_{date_from}_{date_to}_{ts}"
                delete_after = bool(data.get("delete_after", True))
                cleanup_archive(
                    files=files,
                    archive_dir=ARCHIVE_DIR,
                    archive_name=name,
                    base_dir=SNAPSHOT_DIR,
                    job=job,
                    delete_after=delete_after,
                )
            elif action == "compress":
                quality = int(data.get("quality", 70))
                max_width = int(data.get("max_width", 0))
                cleanup_compress(files, job, quality=quality, max_width=max_width)
        except Exception as exc:
            log.exception("Cleanup job failed")
            job.status = "error"
            job.error = str(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/storage/cleanup/status")
def api_cleanup_status():
    with cleanup_lock:
        if current_cleanup is None:
            return jsonify({"job": None})
        return jsonify({"job": current_cleanup.to_dict()})


@app.route("/api/storage/cleanup/cancel", methods=["POST"])
def api_cleanup_cancel():
    with cleanup_lock:
        if current_cleanup and current_cleanup.status in ("pending", "running"):
            current_cleanup.cancel()
            return jsonify({"ok": True})
    return jsonify({"error": "No running cleanup"}), 404


@app.route("/api/archives")
def api_archives():
    return jsonify({"files": list_archives(ARCHIVE_DIR)})


@app.route("/api/archives/<filename>")
def api_archive_download(filename):
    path = ARCHIVE_DIR / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    return send_file(path, as_attachment=True)


@app.route("/api/archives/<filename>", methods=["DELETE"])
def api_archive_delete(filename):
    path = ARCHIVE_DIR / filename
    if path.exists() and path.is_file() and path.suffix.lower() == ".zip":
        path.unlink()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Timelapse Creator</title>
<style>
:root {
  --bg: #1c1c1e; --surface: #2c2c2e; --border: #3a3a3c;
  --text: #f5f5f7; --muted: #8e8e93; --accent: #0a84ff;
  --accent-hover: #409cff; --danger: #ff453a; --success: #30d158;
  --warn: #ff9f0a;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5; padding: 20px;
  max-width: 900px; margin: 0 auto;
}
h1 { font-size: 1.4rem; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
h1 .icon { font-size: 1.6rem; }

.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; margin-bottom: 16px;
}
.card-title { font-weight: 600; font-size: .95rem; margin-bottom: 14px; color: var(--text); }

.btn {
  display: inline-flex; align-items: center; justify-content: center;
  padding: 8px 16px; border: none; border-radius: 8px;
  font-size: .85rem; font-weight: 600; cursor: pointer; transition: all .15s;
  gap: 6px;
}
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-success { background: var(--success); color: #000; }
.btn-danger { background: var(--danger); color: #fff; }
.btn-danger:hover:not(:disabled) { background: #ff6961; }
.btn-sm { padding: 6px 12px; font-size: .8rem; }
.btn-ghost {
  background: transparent; border: 1px solid var(--border); color: var(--muted);
}
.btn-ghost:hover { border-color: var(--accent); color: var(--text); }
.btn-ghost.active { border-color: var(--accent); color: var(--accent); background: rgba(10,132,255,.1); }

.presets { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }

.row { display: flex; gap: 12px; flex-wrap: wrap; }
.row > * { flex: 1; min-width: 120px; }

label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: 4px; margin-top: 10px; }
input, select {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: 8px; font-size: .85rem; width: 100%;
}
input[type=checkbox] { width: auto; margin-right: 6px; }
.checkbox-row { display: flex; align-items: center; margin-top: 12px; font-size: .85rem; }

.stats-bar {
  display: flex; gap: 16px; flex-wrap: wrap; padding: 14px; margin: 14px 0;
  background: var(--bg); border-radius: 8px; border: 1px solid var(--border);
}
.stat { text-align: center; min-width: 80px; }
.stat-val { font-size: 1.3rem; font-weight: 700; }
.stat-lbl { font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
.stat-accent { color: var(--accent); }
.stat-warn { color: var(--warn); }

.sample-strip {
  display: flex; gap: 8px; overflow-x: auto; padding: 10px 0;
  scrollbar-width: thin; scrollbar-color: var(--border) transparent;
}
.sample-strip img {
  width: 100px; height: 75px; object-fit: cover; border-radius: 6px;
  border: 1px solid var(--border); flex-shrink: 0;
}
.sample-strip .sample-label {
  position: absolute; bottom: 2px; left: 2px; right: 2px;
  font-size: .6rem; color: #fff; background: rgba(0,0,0,.6);
  padding: 1px 4px; border-radius: 0 0 5px 5px; text-align: center;
}
.sample-wrap { position: relative; flex-shrink: 0; }

.progress-section { margin: 16px 0; }
.progress-wrap {
  background: var(--bg); border-radius: 6px; height: 8px; overflow: hidden;
  border: 1px solid var(--border);
}
.progress-bar {
  height: 100%; background: var(--accent); border-radius: 6px;
  transition: width .3s ease; width: 0%;
}
.progress-text { font-size: .8rem; color: var(--muted); margin-top: 6px; }
.progress-stats {
  display: flex; gap: 14px; font-size: .8rem; margin-top: 6px; flex-wrap: wrap;
}
.progress-stats span { color: var(--muted); }
.progress-stats .val { color: var(--text); font-weight: 600; }

.output-list { list-style: none; }
.output-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 0; border-bottom: 1px solid var(--border); gap: 12px;
}
.output-item:last-child { border-bottom: none; }
.output-name { font-weight: 500; font-size: .9rem; word-break: break-all; }
.output-meta { font-size: .75rem; color: var(--muted); }
.output-actions { display: flex; gap: 6px; flex-shrink: 0; }

.empty { text-align: center; color: var(--muted); padding: 30px; font-size: .9rem; }

.video-preview {
  width: 100%; max-height: 400px; border-radius: 8px; background: #000;
  margin-top: 12px;
}

.storage-summary {
  display: flex; gap: 14px; flex-wrap: wrap; padding: 12px;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 12px;
}
.storage-bar {
  height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden;
  border: 1px solid var(--border); margin-top: 8px;
}
.storage-bar-fill { height: 100%; background: var(--accent); transition: width .3s; }
.storage-bar-fill.warn { background: var(--warn); }
.storage-bar-fill.danger { background: var(--danger); }

.month-list { display: flex; flex-direction: column; gap: 6px; }
.month-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 12px; background: var(--bg); border-radius: 6px;
  border: 1px solid var(--border); gap: 8px;
}
.month-row .month-name { font-weight: 600; font-size: .9rem; }
.month-row .month-meta { font-size: .75rem; color: var(--muted); }

.cleanup-form { margin-top: 12px; }
.cleanup-form .row { margin-bottom: 8px; }
.cleanup-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; }

.preview-info {
  padding: 10px; margin-top: 10px; background: var(--bg);
  border: 1px solid var(--border); border-radius: 6px;
  font-size: .85rem; color: var(--muted);
}
.preview-info .val { color: var(--text); font-weight: 600; }

.post-cleanup-card {
  background: linear-gradient(135deg, rgba(10,132,255,.08), rgba(48,209,88,.08));
  border: 1px solid var(--accent);
}

.compress-options {
  margin-top: 10px; padding: 10px; background: var(--bg);
  border-radius: 6px; border: 1px solid var(--border);
}
.compress-options-title {
  font-size: .75rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: .5px; margin-bottom: 4px;
}

.tabs { display: flex; gap: 4px; margin-bottom: 14px; border-bottom: 1px solid var(--border); }
.tab {
  padding: 8px 14px; cursor: pointer; font-size: .85rem;
  color: var(--muted); border-bottom: 2px solid transparent;
}
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.tab-content { display: none; }
.tab-content.active { display: block; }

.hidden { display: none !important; }

@keyframes spin { to { transform: rotate(360deg); } }
.spinner {
  width: 16px; height: 16px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite;
  display: inline-block;
}
</style>
</head>
<body>

<h1><span class="icon">&#9201;</span> Timelapse Creator</h1>

<!-- TABS -->
<div class="tabs">
  <div class="tab active" data-tab="generate" onclick="switchTab('generate')">Generuj</div>
  <div class="tab" data-tab="storage" onclick="switchTab('storage')">Pamiec</div>
</div>

<!-- ============================ GENERATE TAB ============================ -->
<div class="tab-content active" id="tab-generate">

<!-- DATE RANGE SELECTION -->
<div class="card">
  <div class="card-title">Zakres dat</div>
  <div class="presets">
    <button class="btn btn-ghost" onclick="setPreset('1W')">Tydzien</button>
    <button class="btn btn-ghost" onclick="setPreset('1M')">Miesiac</button>
    <button class="btn btn-ghost" onclick="setPreset('3M')">3 miesiace</button>
    <button class="btn btn-ghost" onclick="setPreset('6M')">6 miesiecy</button>
    <button class="btn btn-ghost" onclick="setPreset('1Y')">Rok</button>
    <button class="btn btn-ghost" onclick="setPreset('ALL')">Wszystko</button>
  </div>
  <div class="row">
    <div>
      <label>Od</label>
      <input type="date" id="date-from">
    </div>
    <div>
      <label>Do</label>
      <input type="date" id="date-to">
    </div>
    <div>
      <label>Godz. od</label>
      <input type="number" id="hour-from" value="0" min="0" max="23">
    </div>
    <div>
      <label>Godz. do</label>
      <input type="number" id="hour-to" value="24" min="1" max="24">
    </div>
  </div>
  <div style="margin-top:14px">
    <button class="btn btn-primary" onclick="doScan()">Skanuj snapshoty</button>
  </div>
</div>

<!-- SCAN RESULTS -->
<div class="card hidden" id="scan-results">
  <div class="card-title">Wyniki skanowania</div>
  <div class="stats-bar" id="stats-bar"></div>
  <div class="sample-strip" id="sample-strip"></div>
</div>

<!-- GENERATION SETTINGS -->
<div class="card hidden" id="gen-settings">
  <div class="card-title">Ustawienia</div>
  <div class="row">
    <div>
      <label>Docelowy czas wideo (sekundy, 0 = bez limitu)</label>
      <input type="number" id="target-duration" value="60" min="0" max="600">
    </div>
    <div>
      <label>FPS</label>
      <input type="number" id="fps" value="24" min="1" max="60">
    </div>
    <div>
      <label>Rozdzielczosc</label>
      <select id="resolution">
        <option value="720p" selected>720p (1280px)</option>
        <option value="1080p">1080p (1920px)</option>
        <option value="480p">480p (854px)</option>
        <option value="original">Oryginalna</option>
      </select>
    </div>
  </div>
  <div class="checkbox-row">
    <input type="checkbox" id="skip-dark">
    <span>Pomin ciemne klatki (jasnosc &lt; {{ brightness_threshold }})</span>
  </div>
  <div class="checkbox-row">
    <input type="checkbox" id="skip-night" checked>
    <span>Pomin tryb nocny / IR (wykrywa szare klatki z kamery na podczerwien)</span>
  </div>
  <div id="calc-info" style="margin-top:10px; font-size:.8rem; color:var(--muted)"></div>
  <div style="margin-top:16px; display:flex; gap:8px;">
    <button class="btn btn-ghost" onclick="doGenerate(true)">Szybki podglad</button>
    <button class="btn btn-primary" onclick="doGenerate(false)">Generuj timelapse</button>
  </div>
</div>

<!-- PROGRESS -->
<div class="card hidden" id="progress-card">
  <div class="card-title" style="display:flex;align-items:center;gap:8px;">
    <span class="spinner" id="job-spinner"></span>
    <span id="job-status-text">Przetwarzanie...</span>
  </div>
  <div class="progress-section">
    <div class="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    <div class="progress-text" id="progress-text"></div>
    <div class="progress-stats" id="progress-stats"></div>
  </div>
  <div style="margin-top:10px">
    <button class="btn btn-sm btn-danger" id="btn-cancel" onclick="doCancel()">Anuluj</button>
  </div>
  <video class="video-preview hidden" id="preview-video" controls></video>
</div>

<!-- POST-GENERATION CLEANUP -->
<div class="card post-cleanup-card hidden" id="post-cleanup-card">
  <div class="card-title">Optymalizacja pamieci</div>
  <div style="font-size:.85rem; color:var(--muted); margin-bottom:10px;">
    Timelapse gotowy. Mozesz teraz zwolnic miejsce na dysku posprzatajac
    snapshoty z uzytego zakresu (<span id="post-cleanup-range" class="val" style="color:var(--text)"></span>).
  </div>
  <div class="preview-info" id="post-cleanup-info">Ladowanie...</div>
  <div class="cleanup-actions">
    <button class="btn btn-sm btn-ghost" onclick="postCleanup('archive')">Archiwizuj (ZIP)</button>
    <button class="btn btn-sm btn-ghost" onclick="postCleanup('compress')">Skompresuj</button>
    <button class="btn btn-sm btn-danger" onclick="postCleanup('delete')">Usun</button>
    <button class="btn btn-sm btn-ghost" onclick="hide('post-cleanup-card')">Pomin</button>
  </div>
</div>

<!-- OUTPUT LIST -->
<div class="card" id="outputs-card">
  <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
    Gotowe timelapse
    <button class="btn btn-sm btn-ghost" onclick="loadOutputs()">Odswiez</button>
  </div>
  <ul class="output-list" id="output-list">
    <li class="empty">Ladowanie...</li>
  </ul>
</div>

</div><!-- /tab-generate -->

<!-- ============================ STORAGE TAB ============================ -->
<div class="tab-content" id="tab-storage">

<div class="card">
  <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
    Wykorzystanie dysku
    <button class="btn btn-sm btn-ghost" onclick="loadStorage()">Odswiez</button>
  </div>
  <div class="storage-summary" id="storage-summary">
    <div class="empty" style="width:100%">Ladowanie...</div>
  </div>
  <div class="month-list" id="month-list"></div>
</div>

<div class="card">
  <div class="card-title">Sprzatanie zakresu</div>
  <div style="font-size:.8rem; color:var(--muted); margin-bottom:10px;">
    Wybierz zakres dat i godzin, a nastepnie akcje. Mozesz tez kliknac
    miesiac w liscie powyzej zeby wypelnic ten zakres.
  </div>
  <div class="cleanup-form">
    <div class="row">
      <div>
        <label>Od</label>
        <input type="date" id="cleanup-from">
      </div>
      <div>
        <label>Do</label>
        <input type="date" id="cleanup-to">
      </div>
      <div>
        <label>Godz. od</label>
        <input type="number" id="cleanup-hour-from" value="0" min="0" max="23">
      </div>
      <div>
        <label>Godz. do</label>
        <input type="number" id="cleanup-hour-to" value="24" min="1" max="24">
      </div>
    </div>
    <div style="display:flex; gap:8px; margin-top:10px;">
      <button class="btn btn-sm btn-ghost" onclick="loadCleanupPreview()">Sprawdz zakres</button>
    </div>
    <div class="preview-info hidden" id="cleanup-preview"></div>

    <div class="compress-options" id="compress-options">
      <div class="compress-options-title">Opcje kompresji (uzywane tylko dla "Skompresuj")</div>
      <div class="row">
        <div>
          <label>Jakosc JPG (1-100, niska = mniejszy plik)</label>
          <input type="number" id="compress-quality" value="70" min="10" max="100">
        </div>
        <div>
          <label>Maks. szerokosc px (0 = bez zmiany)</label>
          <input type="number" id="compress-maxwidth" value="0" min="0" max="4096">
        </div>
      </div>
    </div>

    <div class="cleanup-actions">
      <button class="btn btn-ghost" onclick="setCleanupAction('archive')">Archiwizuj (ZIP)</button>
      <button class="btn btn-ghost" onclick="setCleanupAction('compress')">Skompresuj</button>
      <button class="btn btn-danger" onclick="setCleanupAction('delete')">Usun</button>
    </div>
  </div>
</div>

<!-- CLEANUP PROGRESS -->
<div class="card hidden" id="cleanup-progress-card">
  <div class="card-title" style="display:flex;align-items:center;gap:8px;">
    <span class="spinner" id="cleanup-spinner"></span>
    <span id="cleanup-status-text">Przetwarzanie...</span>
  </div>
  <div class="progress-section">
    <div class="progress-wrap"><div class="progress-bar" id="cleanup-progress-bar"></div></div>
    <div class="progress-text" id="cleanup-progress-text"></div>
    <div class="progress-stats" id="cleanup-progress-stats"></div>
  </div>
  <div style="margin-top:10px">
    <button class="btn btn-sm btn-danger" id="btn-cleanup-cancel" onclick="cancelCleanup()">Anuluj</button>
  </div>
</div>

<!-- ARCHIVES LIST -->
<div class="card" id="archives-card">
  <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
    Archiwa
    <button class="btn btn-sm btn-ghost" onclick="loadArchives()">Odswiez</button>
  </div>
  <ul class="output-list" id="archive-list">
    <li class="empty">Ladowanie...</li>
  </ul>
</div>

</div><!-- /tab-storage -->

<script>
const BASE = window.location.pathname.replace(/\/+$/, '');
let scanTotal = 0;
let pollTimer = null;
let cleanupPollTimer = null;
let activePreset = null;
let lastJobRange = null;

function fmtBytes(bytes) {
  if (!bytes || bytes < 1024) return (bytes || 0) + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

// --- Tabs ---
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === 'tab-' + name));
  if (name === 'storage') {
    loadStorage();
    loadArchives();
    checkActiveCleanup();
  }
}

async function checkActiveCleanup() {
  const res = await api('/api/storage/cleanup/status');
  if (res.job && (res.job.status === 'pending' || res.job.status === 'running')) {
    show('cleanup-progress-card');
    if (!cleanupPollTimer) startCleanupPolling();
  }
}

// --- Presets ---
function setPreset(key) {
  const now = new Date();
  let from = new Date();

  if (key === '1W') from.setDate(now.getDate() - 7);
  else if (key === '1M') from.setMonth(now.getMonth() - 1);
  else if (key === '3M') from.setMonth(now.getMonth() - 3);
  else if (key === '6M') from.setMonth(now.getMonth() - 6);
  else if (key === '1Y') from.setFullYear(now.getFullYear() - 1);
  else if (key === 'ALL') from = new Date(2020, 0, 1);

  document.getElementById('date-from').value = fmt(from);
  document.getElementById('date-to').value = fmt(now);

  document.querySelectorAll('.presets .btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  activePreset = key;
}

function fmt(d) {
  return d.toISOString().slice(0, 10);
}

// --- Scan ---
async function doScan() {
  const p = getParams();
  if (!p.from || !p.to) return;

  const res = await api(`/api/samples?from=${p.from}&to=${p.to}&hour_from=${p.hour_from}&hour_to=${p.hour_to}`);
  scanTotal = res.total;

  const statsEl = document.getElementById('stats-bar');
  const dur = parseInt(document.getElementById('target-duration').value) || 0;
  const fps = parseInt(document.getElementById('fps').value) || 24;

  statsEl.innerHTML = `
    <div class="stat"><div class="stat-val stat-accent">${res.total.toLocaleString()}</div><div class="stat-lbl">Snapshotow</div></div>
    <div class="stat"><div class="stat-val">${p.from}</div><div class="stat-lbl">Od</div></div>
    <div class="stat"><div class="stat-val">${p.to}</div><div class="stat-lbl">Do</div></div>
    <div class="stat"><div class="stat-val">${p.hour_from}:00-${p.hour_to}:00</div><div class="stat-lbl">Godziny</div></div>
  `;

  const stripEl = document.getElementById('sample-strip');
  if (res.samples.length > 0) {
    stripEl.innerHTML = res.samples.map(s =>
      `<div class="sample-wrap">
        <img src="${BASE}/api/thumbnail/${s.month}/${encodeURIComponent(s.filename)}" loading="lazy" alt="">
        <div class="sample-label">${s.datetime}</div>
      </div>`
    ).join('');
  } else {
    stripEl.innerHTML = '<div class="empty">Brak snapshotow w tym zakresie</div>';
  }

  show('scan-results');
  show('gen-settings');
  updateCalcInfo();
}

function updateCalcInfo() {
  if (!scanTotal) return;
  const dur = parseInt(document.getElementById('target-duration').value) || 0;
  const fps = parseInt(document.getElementById('fps').value) || 24;
  const el = document.getElementById('calc-info');

  if (dur > 0) {
    const needed = dur * fps;
    const skip = Math.max(1, Math.floor(scanTotal / needed));
    const actual = Math.ceil(scanTotal / skip);
    const actualDur = (actual / fps).toFixed(1);
    el.textContent = `${scanTotal.toLocaleString()} klatek -> co ${skip}. klatka -> ${actual.toLocaleString()} klatek -> ~${actualDur}s przy ${fps}fps`;
  } else {
    const totalDur = (scanTotal / fps).toFixed(1);
    el.textContent = `${scanTotal.toLocaleString()} klatek -> ~${totalDur}s przy ${fps}fps (wszystkie klatki)`;
  }
}

document.getElementById('target-duration').addEventListener('input', updateCalcInfo);
document.getElementById('fps').addEventListener('input', updateCalcInfo);

// --- Generate ---
async function doGenerate(preview) {
  const p = getParams();
  if (!p.from || !p.to) return;

  const body = {
    from: p.from, to: p.to,
    hour_from: p.hour_from, hour_to: p.hour_to,
    fps: parseInt(document.getElementById('fps').value) || 24,
    resolution: document.getElementById('resolution').value,
    target_duration: parseInt(document.getElementById('target-duration').value) || 0,
    skip_dark: document.getElementById('skip-dark').checked,
    skip_night: document.getElementById('skip-night').checked,
    preview: preview,
  };

  const res = await api('/api/generate', 'POST', body);
  if (res.error) { alert(res.error); return; }

  hide('post-cleanup-card');
  show('progress-card');
  document.getElementById('preview-video').classList.add('hidden');
  document.getElementById('btn-cancel').classList.remove('hidden');
  document.getElementById('job-spinner').classList.remove('hidden');
  startPolling();
}

async function doCancel() {
  await api('/api/job/cancel', 'POST');
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollJob, 1500);
  pollJob();
}

async function pollJob() {
  const res = await api('/api/job');
  if (!res.job) return;
  const j = res.job;

  document.getElementById('progress-bar').style.width = j.progress + '%';
  document.getElementById('progress-text').textContent = j.message;
  document.getElementById('job-status-text').textContent =
    j.status === 'validating' ? 'Walidacja klatek...' :
    j.status === 'generating' ? 'Generowanie wideo...' :
    j.status === 'done' ? 'Gotowe!' :
    j.status === 'error' ? 'Blad!' :
    j.status === 'cancelled' ? 'Anulowane' : 'Przetwarzanie...';

  document.getElementById('progress-stats').innerHTML = `
    <span>Przetworzonych: <span class="val">${j.processed_frames}/${j.total_frames}</span></span>
    ${j.skipped_corrupt > 0 ? `<span>Uszkodzonych: <span class="val">${j.skipped_corrupt}</span></span>` : ''}
    ${j.skipped_dark > 0 ? `<span>Ciemnych: <span class="val">${j.skipped_dark}</span></span>` : ''}
    ${j.skipped_nightmode > 0 ? `<span>Tryb nocny: <span class="val">${j.skipped_nightmode}</span></span>` : ''}
    ${j.skipped_sampling > 0 ? `<span>Sampling: <span class="val">co ${Math.round(j.total_frames / (j.used_frames || 1))}.</span></span>` : ''}
    <span>Uzytych: <span class="val">${j.used_frames}</span></span>
  `;

  if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
    clearInterval(pollTimer);
    pollTimer = null;
    document.getElementById('btn-cancel').classList.add('hidden');
    document.getElementById('job-spinner').classList.add('hidden');

    if (j.status === 'done' && j.output_file) {
      const video = document.getElementById('preview-video');
      video.src = BASE + '/api/outputs/' + encodeURIComponent(j.output_file) + '/stream';
      video.classList.remove('hidden');
      loadOutputs();

      lastJobRange = j.source_range || null;
      if (lastJobRange && !lastJobRange.preview) {
        showPostCleanupCard();
      }
    }
    if (j.status === 'error') {
      document.getElementById('progress-text').textContent = 'Blad: ' + (j.error || 'Unknown');
    }
  }
}

// --- Post-generation cleanup ---
async function showPostCleanupCard() {
  if (!lastJobRange) return;
  const r = lastJobRange;
  document.getElementById('post-cleanup-range').textContent =
    `${r.from} - ${r.to}, godz. ${r.hour_from}:00-${r.hour_to}:00`;
  show('post-cleanup-card');

  const info = document.getElementById('post-cleanup-info');
  info.textContent = 'Liczenie...';
  const q = `from=${r.from}&to=${r.to}&hour_from=${r.hour_from}&hour_to=${r.hour_to}`;
  const res = await api('/api/storage/preview?' + q);
  if (res.error) {
    info.textContent = 'Blad: ' + res.error;
    return;
  }
  info.innerHTML = `Snapshotow: <span class="val">${res.count.toLocaleString()}</span> &middot;
                    Rozmiar: <span class="val">${fmtBytes(res.size_bytes)}</span>`;
}

async function postCleanup(action) {
  if (!lastJobRange) return;
  const r = lastJobRange;
  await runCleanup(action, r.from, r.to, r.hour_from, r.hour_to);
  hide('post-cleanup-card');
}

// --- Storage tab ---
async function loadStorage() {
  const res = await api('/api/storage');
  const summary = document.getElementById('storage-summary');
  const list = document.getElementById('month-list');

  if (!res.exists) {
    summary.innerHTML = '<div class="empty" style="width:100%">Folder snapshotow nie istnieje</div>';
    list.innerHTML = '';
    return;
  }

  const usedPct = res.total_disk_bytes
    ? ((res.total_disk_bytes - res.free_bytes) / res.total_disk_bytes * 100)
    : 0;
  let barClass = '';
  if (usedPct > 90) barClass = 'danger';
  else if (usedPct > 75) barClass = 'warn';

  summary.innerHTML = `
    <div class="stat"><div class="stat-val stat-accent">${res.total_files.toLocaleString()}</div><div class="stat-lbl">Snapshotow</div></div>
    <div class="stat"><div class="stat-val">${fmtBytes(res.total_bytes)}</div><div class="stat-lbl">Zajmuja</div></div>
    <div class="stat"><div class="stat-val">${fmtBytes(res.free_bytes)}</div><div class="stat-lbl">Wolne na dysku</div></div>
    <div style="flex:1; min-width:200px;">
      <div style="font-size:.75rem; color:var(--muted)">Wykorzystanie dysku: ${usedPct.toFixed(0)}%</div>
      <div class="storage-bar"><div class="storage-bar-fill ${barClass}" style="width:${usedPct}%"></div></div>
    </div>
  `;

  if (!res.months.length) {
    list.innerHTML = '<div class="empty">Brak danych</div>';
    return;
  }

  list.innerHTML = res.months.map(m => {
    const [y, mo] = m.month.split('-');
    return `<div class="month-row" onclick="selectMonth('${m.month}')">
      <div>
        <div class="month-name">${m.month}</div>
        <div class="month-meta">${m.count.toLocaleString()} plikow &middot; ${fmtBytes(m.size_bytes)}</div>
      </div>
      <button class="btn btn-sm btn-ghost" onclick="event.stopPropagation(); selectMonth('${m.month}')">Wybierz</button>
    </div>`;
  }).join('');
}

function selectMonth(month) {
  const [y, mo] = month.split('-');
  const first = `${y}-${mo}-01`;
  const lastDay = new Date(parseInt(y), parseInt(mo), 0).getDate();
  const last = `${y}-${mo}-${String(lastDay).padStart(2, '0')}`;
  document.getElementById('cleanup-from').value = first;
  document.getElementById('cleanup-to').value = last;
  document.getElementById('cleanup-hour-from').value = 0;
  document.getElementById('cleanup-hour-to').value = 24;
  loadCleanupPreview();
}

function getCleanupRange() {
  return {
    from: document.getElementById('cleanup-from').value,
    to: document.getElementById('cleanup-to').value,
    hour_from: parseInt(document.getElementById('cleanup-hour-from').value) || 0,
    hour_to: parseInt(document.getElementById('cleanup-hour-to').value) || 24,
  };
}

async function loadCleanupPreview() {
  const r = getCleanupRange();
  if (!r.from || !r.to) {
    alert('Wybierz zakres dat');
    return;
  }
  const el = document.getElementById('cleanup-preview');
  el.classList.remove('hidden');
  el.textContent = 'Liczenie...';

  const q = `from=${r.from}&to=${r.to}&hour_from=${r.hour_from}&hour_to=${r.hour_to}`;
  const res = await api('/api/storage/preview?' + q);
  if (res.error) { el.textContent = 'Blad: ' + res.error; return; }

  el.innerHTML = `Snapshotow do przetworzenia: <span class="val">${res.count.toLocaleString()}</span> &middot;
                  Rozmiar: <span class="val">${fmtBytes(res.size_bytes)}</span>`;
}

function setCleanupAction(action) {
  const r = getCleanupRange();
  if (!r.from || !r.to) { alert('Wybierz zakres dat'); return; }
  runCleanup(action, r.from, r.to, r.hour_from, r.hour_to);
}

async function runCleanup(action, from, to, hour_from, hour_to) {
  const labels = {
    delete: 'Na pewno PERMANENTNIE usunac wybrane snapshoty?',
    archive: 'Spakowac wybrane snapshoty do ZIP i usunac oryginaly?',
    compress: 'Skompresowac wybrane snapshoty (re-encode JPG)?',
  };
  if (!confirm(labels[action] || 'Kontynuowac?')) return;

  const body = { action, from, to, hour_from, hour_to };
  if (action === 'compress') {
    body.quality = parseInt(document.getElementById('compress-quality').value) || 70;
    body.max_width = parseInt(document.getElementById('compress-maxwidth').value) || 0;
  }

  const res = await api('/api/storage/cleanup', 'POST', body);
  if (res.error) { alert(res.error); return; }

  switchTab('storage');
  show('cleanup-progress-card');
  document.getElementById('cleanup-spinner').classList.remove('hidden');
  document.getElementById('btn-cleanup-cancel').classList.remove('hidden');
  document.getElementById('cleanup-progress-bar').style.width = '0%';
  startCleanupPolling();
}

function startCleanupPolling() {
  if (cleanupPollTimer) clearInterval(cleanupPollTimer);
  cleanupPollTimer = setInterval(pollCleanup, 1500);
  pollCleanup();
}

async function pollCleanup() {
  const res = await api('/api/storage/cleanup/status');
  if (!res.job) return;
  const j = res.job;

  document.getElementById('cleanup-progress-bar').style.width = j.progress + '%';
  document.getElementById('cleanup-progress-text').textContent = j.message;
  document.getElementById('cleanup-status-text').textContent =
    j.status === 'pending' ? 'Przygotowanie...' :
    j.status === 'running' ?
      (j.action === 'delete' ? 'Usuwanie plikow...' :
       j.action === 'archive' ? 'Archiwizacja...' :
       j.action === 'compress' ? 'Kompresja...' : 'Przetwarzanie...') :
    j.status === 'done' ? 'Gotowe!' :
    j.status === 'error' ? 'Blad' :
    j.status === 'cancelled' ? 'Anulowane' : 'Przetwarzanie...';

  document.getElementById('cleanup-progress-stats').innerHTML = `
    <span>Plikow: <span class="val">${j.processed_files}/${j.total_files}</span></span>
    ${j.failed_files > 0 ? `<span>Bledne: <span class="val">${j.failed_files}</span></span>` : ''}
    <span>Zwolnione: <span class="val">${fmtBytes(j.bytes_freed)}</span></span>
  `;

  if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
    clearInterval(cleanupPollTimer);
    cleanupPollTimer = null;
    document.getElementById('cleanup-spinner').classList.add('hidden');
    document.getElementById('btn-cleanup-cancel').classList.add('hidden');
    if (j.status === 'error') {
      document.getElementById('cleanup-progress-text').textContent = 'Blad: ' + (j.error || 'Unknown');
    }
    loadStorage();
    loadArchives();
  }
}

async function cancelCleanup() {
  await api('/api/storage/cleanup/cancel', 'POST');
}

// --- Archives ---
async function loadArchives() {
  const res = await api('/api/archives');
  const ul = document.getElementById('archive-list');
  if (!res.files || !res.files.length) {
    ul.innerHTML = '<li class="empty">Brak archiwow</li>';
    return;
  }
  ul.innerHTML = res.files.map(f => `
    <li class="output-item">
      <div>
        <div class="output-name">${f.name}</div>
        <div class="output-meta">${f.size_mb} MB &middot; ${f.created}</div>
      </div>
      <div class="output-actions">
        <a class="btn btn-sm btn-primary" href="${BASE}/api/archives/${encodeURIComponent(f.name)}" download>Pobierz</a>
        <button class="btn btn-sm btn-danger" onclick="deleteArchive('${f.name}')">Usun</button>
      </div>
    </li>
  `).join('');
}

async function deleteArchive(name) {
  if (!confirm('Usunac archiwum ' + name + '?')) return;
  await api('/api/archives/' + encodeURIComponent(name), 'DELETE');
  loadArchives();
  loadStorage();
}

// --- Outputs ---
async function loadOutputs() {
  const res = await api('/api/outputs');
  const ul = document.getElementById('output-list');
  if (!res.files || !res.files.length) {
    ul.innerHTML = '<li class="empty">Brak wygenerowanych timelapsow</li>';
    return;
  }
  ul.innerHTML = res.files.map(f => `
    <li class="output-item">
      <div>
        <div class="output-name">${f.name}</div>
        <div class="output-meta">${f.size_mb} MB &middot; ${f.created}</div>
      </div>
      <div class="output-actions">
        <button class="btn btn-sm btn-ghost" onclick="playOutput('${f.name}')">Odtworz</button>
        <a class="btn btn-sm btn-primary" href="${BASE}/api/outputs/${encodeURIComponent(f.name)}" download>Pobierz</a>
        <button class="btn btn-sm btn-danger" onclick="deleteOutput('${f.name}')">Usun</button>
      </div>
    </li>
  `).join('');
}

function playOutput(name) {
  show('progress-card');
  document.getElementById('job-spinner').classList.add('hidden');
  document.getElementById('job-status-text').textContent = name;
  document.getElementById('btn-cancel').classList.add('hidden');
  document.getElementById('progress-stats').innerHTML = '';
  document.getElementById('progress-text').textContent = '';
  document.getElementById('progress-bar').style.width = '0%';
  const video = document.getElementById('preview-video');
  video.src = BASE + '/api/outputs/' + encodeURIComponent(name) + '/stream';
  video.classList.remove('hidden');
}

async function deleteOutput(name) {
  if (!confirm('Usunac ' + name + '?')) return;
  await api('/api/outputs/' + encodeURIComponent(name), 'DELETE');
  loadOutputs();
}

// --- Helpers ---
function getParams() {
  return {
    from: document.getElementById('date-from').value,
    to: document.getElementById('date-to').value,
    hour_from: parseInt(document.getElementById('hour-from').value) || 0,
    hour_to: parseInt(document.getElementById('hour-to').value) || 24,
  };
}

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }

async function api(url, method, body) {
  const opts = { method: method || 'GET' };
  if (body) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify(body);
  }
  try {
    const r = await fetch(BASE + url, opts);
    return await r.json();
  } catch(e) {
    console.error('API error:', e);
    return { error: e.message };
  }
}

// --- Init ---
const today = new Date();
document.getElementById('date-to').value = fmt(today);
const monthAgo = new Date();
monthAgo.setMonth(today.getMonth() - 1);
document.getElementById('date-from').value = fmt(monthAgo);

loadOutputs();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("INGRESS_PORT", 8099))
    app.run(host="0.0.0.0", port=port)
