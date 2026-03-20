import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

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
FILE_PATTERN = os.environ.get("FILE_PATTERN", "*.jpg")
MAX_THREADS = int(os.environ.get("MAX_THREADS", "2"))
BRIGHTNESS_THRESHOLD = int(os.environ.get("BRIGHTNESS_THRESHOLD", "30"))
THUMB_DIR = Path("/data/.thumbs")

app = Flask(__name__)

job_lock = threading.Lock()
current_job: TimelapseJob | None = None


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

    data = request.get_json(force=True)
    date_from = data.get("from", "")
    date_to = data.get("to", "")
    hour_from = int(data.get("hour_from", 0))
    hour_to = int(data.get("hour_to", 24))
    fps = int(data.get("fps", 24))
    resolution = data.get("resolution", "720p")
    target_duration = int(data.get("target_duration", 0))
    skip_dark = bool(data.get("skip_dark", False))
    is_preview = bool(data.get("preview", False))

    if not date_from or not date_to:
        return jsonify({"error": "from and to required"}), 400

    images = scan_snapshots(SNAPSHOT_DIR, FILE_PATTERN, date_from, date_to, hour_from, hour_to)
    if not images:
        return jsonify({"error": "No snapshots found for this range"}), 404

    job = TimelapseJob()
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
                )
            else:
                skip_every = 1
                if target_duration > 0:
                    needed = target_duration * fps
                    if len(images) > needed:
                        skip_every = max(1, len(images) // needed)

                generate_timelapse(
                    images, out_path, job,
                    fps=fps, resolution=resolution, max_threads=MAX_THREADS,
                    skip_dark=skip_dark, brightness_threshold=BRIGHTNESS_THRESHOLD,
                    skip_every=skip_every,
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
        return jsonify({"job": current_job.to_dict()})


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
    <input type="checkbox" id="skip-dark" checked>
    <span>Pomin ciemne klatki (nocne) - prog jasnosci: {{ brightness_threshold }}</span>
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

<script>
const BASE = window.location.pathname.replace(/\/+$/, '');
let scanTotal = 0;
let pollTimer = null;
let activePreset = null;

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
    preview: preview,
  };

  const res = await api('/api/generate', 'POST', body);
  if (res.error) { alert(res.error); return; }

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
    <span>Uszkodzonych: <span class="val">${j.skipped_corrupt}</span></span>
    <span>Ciemnych: <span class="val">${j.skipped_dark}</span></span>
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
    }
    if (j.status === 'error') {
      document.getElementById('progress-text').textContent = 'Blad: ' + (j.error || 'Unknown');
    }
  }
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
