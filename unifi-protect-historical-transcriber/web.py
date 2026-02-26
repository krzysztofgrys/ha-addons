import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

from main import EXPORT_AUDIO_DIR, HOURS_BACK, WHISPER_ENABLED, run_pipeline

app = Flask(__name__)


job_lock = threading.Lock()
current_job = {
    "running": False,
    "status": [],
    "result": None,
    "started_at": None,
}

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UniFi Protect Transcriber</title>
<style>
  :root {
    --bg: #1c1c1e; --surface: #2c2c2e; --border: #3a3a3c;
    --text: #f5f5f7; --muted: #8e8e93; --accent: #0a84ff;
    --accent-hover: #409cff; --danger: #ff453a; --success: #30d158;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; padding: 20px; }
  h1 { font-size: 1.5rem; margin-bottom: 24px; }
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
  .tab { padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent;
         color: var(--muted); font-weight: 500; transition: all .2s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .panel { display: none; }
  .panel.active { display: block; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
          padding: 20px; margin-bottom: 16px; }
  .btn { display: inline-block; padding: 10px 20px; border: none; border-radius: 8px;
         font-size: .9rem; font-weight: 600; cursor: pointer; transition: all .2s; }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-danger { background: var(--danger); color: white; }
  .btn-sm { padding: 6px 14px; font-size: .8rem; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  input, select { background: var(--bg); border: 1px solid var(--border); color: var(--text);
                  padding: 8px 12px; border-radius: 8px; font-size: .9rem; width: 100%; }
  label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: 4px; margin-top: 12px; }
  .file-list { list-style: none; }
  .file-list li { display: flex; align-items: center; justify-content: space-between;
                  padding: 10px 0; border-bottom: 1px solid var(--border); }
  .file-list li:last-child { border-bottom: none; }
  .file-name { font-weight: 500; }
  .file-meta { color: var(--muted); font-size: .8rem; }
  .status-log { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
                padding: 12px; max-height: 300px; overflow-y: auto; font-family: monospace;
                font-size: .8rem; white-space: pre-wrap; color: var(--muted); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .75rem; font-weight: 600; }
  .badge-running { background: var(--accent); color: white; }
  .badge-done { background: var(--success); color: black; }
  .badge-error { background: var(--danger); color: white; }
  .transcript-box { background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
                    padding: 16px; margin-top: 8px; font-size: .9rem; line-height: 1.6;
                    max-height: 400px; overflow-y: auto; white-space: pre-wrap; }
  .empty { text-align: center; color: var(--muted); padding: 40px; }
  .row { display: flex; gap: 12px; }
  .row > * { flex: 1; }
</style>
</head>
<body>
<h1>UniFi Protect Transcriber</h1>

<div class="tabs">
  <div class="tab active" data-tab="files">Pliki</div>
  <div class="tab" data-tab="process">Nowe przetwarzanie</div>
  <div class="tab" data-tab="transcripts">Transkrypcje</div>
</div>

<div id="files" class="panel active">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong>Pliki audio</strong>
      <button class="btn btn-sm btn-primary" onclick="loadFiles()">Odswiez</button>
    </div>
    <ul class="file-list" id="file-list"><li class="empty">Ladowanie...</li></ul>
  </div>
</div>

<div id="process" class="panel">
  <div class="card">
    <strong>Uruchom przetwarzanie</strong>
    <div class="row">
      <div>
        <label>Tryb</label>
        <select id="mode">
          <option value="download">Pobierz z UniFi Protect</option>
          <option value="local">Przetworz lokalne MP4</option>
        </select>
      </div>
      <div>
        <label>Godziny wstecz</label>
        <input type="number" id="hours" value="{{ hours_back }}" min="1" max="168">
      </div>
    </div>
    <div id="local-dir-row" style="display:none">
      <label>Katalog z MP4</label>
      <input type="text" id="local-dir" value="{{ export_dir }}" placeholder="/share/...">
    </div>
    <label>
      <input type="checkbox" id="do-transcribe" {{ 'checked' if whisper_enabled else '' }}>
      Uruchom transkrypcje (Whisper)
    </label>
    <div style="margin-top:16px">
      <button class="btn btn-primary" id="btn-start" onclick="startJob()">Rozpocznij</button>
    </div>
  </div>

  <div class="card" id="job-card" style="display:none">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <strong>Status</strong>
      <span class="badge" id="job-badge">...</span>
    </div>
    <div class="status-log" id="job-log"></div>
  </div>
</div>

<div id="transcripts" class="panel">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong>Transkrypcje</strong>
      <button class="btn btn-sm btn-primary" onclick="loadTranscripts()">Odswiez</button>
    </div>
    <div id="transcript-list"><div class="empty">Ladowanie...</div></div>
  </div>
</div>

<script>
const BASE = window.location.pathname.replace(/\/+$/, '');

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.tab).classList.add('active');
  });
});

document.getElementById('mode').addEventListener('change', e => {
  document.getElementById('local-dir-row').style.display = e.target.value === 'local' ? 'block' : 'none';
});

async function loadFiles() {
  const r = await fetch(BASE + '/api/files');
  const data = await r.json();
  const ul = document.getElementById('file-list');
  if (!data.files.length) { ul.innerHTML = '<li class="empty">Brak plikow audio</li>'; return; }
  ul.innerHTML = data.files.map(f => `<li>
    <div>
      <div class="file-name">${f.name}</div>
      <div class="file-meta">${f.size_mb} MB &middot; ${f.modified}</div>
    </div>
    <div style="display:flex;gap:6px">
      <button class="btn btn-sm btn-primary" onclick="location.href=BASE+'/api/download/'+encodeURIComponent(f.name)">Pobierz</button>
      <button class="btn btn-sm btn-danger" onclick="deleteFile('${f.name}')">Usun</button>
    </div>
  </li>`).join('');
}

async function deleteFile(name) {
  if (!confirm('Usunac ' + name + '?')) return;
  await fetch(BASE + '/api/files/' + encodeURIComponent(name), {method:'DELETE'});
  loadFiles();
}

async function loadTranscripts() {
  const r = await fetch(BASE + '/api/transcripts');
  const data = await r.json();
  const div = document.getElementById('transcript-list');
  if (!data.transcripts.length) { div.innerHTML = '<div class="empty">Brak transkrypcji</div>'; return; }
  div.innerHTML = data.transcripts.map(t => `<div class="card" style="margin-bottom:12px">
    <div style="display:flex;justify-content:space-between">
      <strong>${t.name}</strong>
      <span class="file-meta">${t.modified}</span>
    </div>
    <div class="transcript-box">${t.text}</div>
  </div>`).join('');
}

let pollTimer = null;
async function startJob() {
  const mode = document.getElementById('mode').value;
  const hours = document.getElementById('hours').value;
  const localDir = document.getElementById('local-dir').value;
  const doTranscribe = document.getElementById('do-transcribe').checked;

  document.getElementById('btn-start').disabled = true;
  document.getElementById('job-card').style.display = 'block';
  document.getElementById('job-log').textContent = '';
  document.getElementById('job-badge').textContent = 'Uruchomiono...';
  document.getElementById('job-badge').className = 'badge badge-running';

  await fetch(BASE + '/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode, hours: parseInt(hours), local_dir: localDir, do_transcribe: doTranscribe})
  });

  pollTimer = setInterval(pollStatus, 2000);
}

async function pollStatus() {
  const r = await fetch(BASE + '/api/status');
  const data = await r.json();
  const logEl = document.getElementById('job-log');
  logEl.textContent = data.status.join('\n');
  logEl.scrollTop = logEl.scrollHeight;

  if (!data.running) {
    clearInterval(pollTimer);
    document.getElementById('btn-start').disabled = false;
    const badge = document.getElementById('job-badge');
    if (data.result && data.result.ok) {
      badge.textContent = 'Zakonczone';
      badge.className = 'badge badge-done';
      if (data.result.transcription) {
        logEl.textContent += '\n\n--- TRANSKRYPCJA ---\n' + data.result.transcription;
      }
    } else {
      badge.textContent = 'Blad';
      badge.className = 'badge badge-error';
    }
    logEl.scrollTop = logEl.scrollHeight;
    loadFiles();
    loadTranscripts();
  }
}

loadFiles();
loadTranscripts();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        hours_back=HOURS_BACK,
        export_dir=str(EXPORT_AUDIO_DIR),
        whisper_enabled=WHISPER_ENABLED,
    )


@app.route("/api/files")
def api_files():
    EXPORT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(EXPORT_AUDIO_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix in (".wav", ".mp4"):
            st = p.stat()
            files.append({
                "name": p.name,
                "size_mb": f"{st.st_size / 1024 / 1024:.1f}",
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify({"files": files})


@app.route("/api/download/<filename>")
def api_download(filename):
    path = EXPORT_AUDIO_DIR / filename
    if not path.exists() or not path.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True)


@app.route("/api/files/<filename>", methods=["DELETE"])
def api_delete_file(filename):
    path = EXPORT_AUDIO_DIR / filename
    txt = path.with_suffix(".txt")
    for f in (path, txt):
        if f.exists():
            f.unlink()
    return jsonify({"ok": True})


@app.route("/api/transcripts")
def api_transcripts():
    EXPORT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    transcripts = []
    for p in sorted(EXPORT_AUDIO_DIR.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            transcripts.append({
                "name": p.name,
                "text": text,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify({"transcripts": transcripts})


@app.route("/api/start", methods=["POST"])
def api_start():
    with job_lock:
        if current_job["running"]:
            return jsonify({"error": "Job already running"}), 409

    data = request.get_json(force=True)
    mode = data.get("mode", "download")
    hours = data.get("hours", HOURS_BACK)
    local_dir = data.get("local_dir", "")
    do_transcribe = data.get("do_transcribe", WHISPER_ENABLED)

    def _run():
        with job_lock:
            current_job["running"] = True
            current_job["status"] = []
            current_job["result"] = None
            current_job["started_at"] = datetime.now(timezone.utc).isoformat()

        def _cb(msg):
            with job_lock:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                current_job["status"].append(f"[{ts}] {msg}")

        result = run_pipeline(
            mode=mode,
            local_dir=local_dir if mode == "local" else None,
            hours=hours,
            do_transcribe=do_transcribe,
            status_callback=_cb,
        )

        with job_lock:
            current_job["running"] = False
            current_job["result"] = result

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Job started"})


@app.route("/api/status")
def api_status():
    with job_lock:
        return jsonify({
            "running": current_job["running"],
            "status": list(current_job["status"]),
            "result": current_job["result"],
            "started_at": current_job["started_at"],
        })


if __name__ == "__main__":
    port = int(os.environ.get("INGRESS_PORT", 8099))
    app.run(host="0.0.0.0", port=port)
