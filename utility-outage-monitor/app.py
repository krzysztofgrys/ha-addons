"""Utility Outage Monitor -- Flask web UI and API."""

import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, render_template_string

from main import (
    CHECK_INTERVAL,
    CITY_NAME,
    ENABLE_MPWIK,
    ENABLE_TAURON,
    HOUSE_NUMBER,
    MOBILE_NOTIFY_SERVICES,
    NOTIFY_MOBILE,
    NOTIFY_PERSISTENT,
    STREET_NAME,
    get_alerts,
    get_state,
    init_state,
    load_history,
    resolve_gaid,
    run_check,
    tauron_lookup_city,
    tauron_lookup_street,
)

app = Flask(__name__)

scheduler = BackgroundScheduler(daemon=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ---------------------------------------------------------------------------
# API: alerts
# ---------------------------------------------------------------------------

@app.route("/api/alerts")
def api_alerts():
    alerts = get_alerts()
    matched = [a for a in alerts if a.get("matched")]
    other = [a for a in alerts if not a.get("matched")]
    return jsonify({"matched": matched, "other": other, "total": len(alerts)})


@app.route("/api/status")
def api_status():
    state = get_state()
    return jsonify({
        "last_check": state["last_check"],
        "next_check": state["next_check"],
        "gaid": state["gaid"],
        "api_health": state["api_health"],
        "check_running": state["check_running"],
        "alert_count": len(state["alerts"]),
        "config": {
            "city_name": CITY_NAME,
            "street_name": STREET_NAME,
            "house_number": HOUSE_NUMBER,
            "check_interval": CHECK_INTERVAL,
            "enable_tauron": ENABLE_TAURON,
            "enable_mpwik": ENABLE_MPWIK,
            "notify_mobile": NOTIFY_MOBILE,
            "mobile_notify_services": MOBILE_NOTIFY_SERVICES,
            "notify_persistent": NOTIFY_PERSISTENT,
        },
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=run_check, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Check started"})


@app.route("/api/resolve")
def api_resolve():
    city = request.args.get("city", CITY_NAME)
    street = request.args.get("street", STREET_NAME)
    result = {}
    try:
        c = tauron_lookup_city(city)
        result["city"] = c
        if c:
            s = tauron_lookup_street(street, c["GAID"])
            result["street"] = s
    except Exception as exc:
        result["error"] = str(exc)
    return jsonify(result)


# ---------------------------------------------------------------------------
# API: history
# ---------------------------------------------------------------------------

@app.route("/api/history")
def api_history():
    history = load_history()
    source_filter = request.args.get("source")
    date_from = request.args.get("from")
    date_to = request.args.get("to")

    if source_filter:
        history = [h for h in history if h.get("source") == source_filter]
    if date_from:
        history = [h for h in history if (h.get("start_date") or "") >= date_from]
    if date_to:
        history = [h for h in history if (h.get("start_date") or "") <= date_to]

    history.sort(key=lambda x: x.get("start_date") or "", reverse=True)
    return jsonify({"history": history, "total": len(history)})


@app.route("/api/history/stats")
def api_history_stats():
    history = load_history()
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")

    this_month = [h for h in history if (h.get("start_date") or "").startswith(current_month)]

    durations = [h["duration_hours"] for h in history if h.get("duration_hours") is not None]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0

    current_year = now.strftime("%Y")
    year_durations = [
        h["duration_hours"]
        for h in history
        if h.get("duration_hours") is not None and (h.get("start_date") or "").startswith(current_year)
    ]
    longest = max(year_durations) if year_durations else 0

    days_since_last = None
    if history:
        sorted_h = sorted(history, key=lambda x: x.get("resolved_at") or x.get("end_date") or "", reverse=True)
        last_date_str = sorted_h[0].get("resolved_at") or sorted_h[0].get("end_date")
        if last_date_str:
            try:
                last_dt = datetime.fromisoformat(last_date_str.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days_since_last = max(0, (now - last_dt).days)
            except (ValueError, TypeError):
                pass

    monthly = defaultdict(lambda: {"tauron": 0, "mpwik": 0})
    for h in history:
        sd = h.get("start_date", "")
        if len(sd) >= 7:
            month_key = sd[:7]
            src = h.get("source", "unknown")
            if src in ("tauron", "mpwik"):
                monthly[month_key][src] += 1

    months_sorted = sorted(monthly.keys())[-12:]
    chart_data = {
        "labels": months_sorted,
        "tauron": [monthly[m]["tauron"] for m in months_sorted],
        "mpwik": [monthly[m]["mpwik"] for m in months_sorted],
    }

    return jsonify({
        "this_month_count": len(this_month),
        "avg_duration_hours": avg_duration,
        "longest_hours": longest,
        "days_since_last": days_since_last,
        "chart": chart_data,
    })


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Utility Outage Monitor</title>
<style>
:root {
  --bg: #1c1c1e; --surface: #2c2c2e; --border: #3a3a3c;
  --text: #f5f5f7; --muted: #8e8e93; --accent: #0a84ff;
  --accent-hover: #409cff; --danger: #ff453a; --success: #30d158;
  --warn: #ff9f0a; --power: #ff9f0a; --water: #32ade6;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5; padding: 20px;
  max-width: 900px; margin: 0 auto;
}
h1 { font-size: 1.4rem; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
h1 .icon { font-size: 1.6rem; }

.tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
.tab {
  padding: 10px 20px; cursor: pointer; border-bottom: 2px solid transparent;
  color: var(--muted); font-weight: 500; transition: all .2s; position: relative; user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab .dot {
  display: none; width: 8px; height: 8px; background: var(--danger); border-radius: 50%;
  position: absolute; top: 6px; right: 6px; animation: pulse 1.5s ease-in-out infinite;
}
.tab .dot.visible { display: block; }
.panel { display: none; }
.panel.active { display: block; }

.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; margin-bottom: 16px;
}
.card-title { font-weight: 600; font-size: .95rem; margin-bottom: 14px; color: var(--text); }

.btn {
  display: inline-flex; align-items: center; justify-content: center;
  padding: 8px 16px; border: none; border-radius: 8px;
  font-size: .85rem; font-weight: 600; cursor: pointer; transition: all .15s; gap: 6px;
}
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-sm { padding: 6px 12px; font-size: .8rem; }
.btn-ghost {
  background: transparent; border: 1px solid var(--border); color: var(--muted);
}
.btn-ghost:hover { border-color: var(--accent); color: var(--text); }
.btn-ghost.active { border-color: var(--accent); color: var(--accent); background: rgba(10,132,255,.1); }

.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: .75rem; font-weight: 600; text-transform: uppercase;
}
.badge-power { background: rgba(255,159,10,.15); color: var(--power); }
.badge-water { background: rgba(50,173,230,.15); color: var(--water); }
.badge-ok { background: rgba(48,209,88,.15); color: var(--success); }
.badge-error { background: rgba(255,69,58,.15); color: var(--danger); }
.badge-pending { background: rgba(142,142,147,.15); color: var(--muted); }

.stats-bar {
  display: flex; gap: 16px; flex-wrap: wrap; padding: 14px; margin-bottom: 16px;
  background: var(--bg); border-radius: 8px; border: 1px solid var(--border);
}
.stat { text-align: center; min-width: 80px; flex: 1; }
.stat-val { font-size: 1.3rem; font-weight: 700; }
.stat-lbl { font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }

.summary-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 16px; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}
.summary-bar .counts { display: flex; gap: 10px; flex: 1; }
.summary-bar .meta { color: var(--muted); font-size: .8rem; }

.alert-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px; margin-bottom: 10px; display: flex; gap: 14px;
  border-left: 4px solid var(--border); transition: border-color .2s;
}
.alert-card.source-tauron { border-left-color: var(--power); }
.alert-card.source-mpwik { border-left-color: var(--water); }
.alert-card .alert-body { flex: 1; min-width: 0; }
.alert-card .alert-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.alert-card .alert-dates { font-size: .85rem; color: var(--muted); margin-bottom: 4px; }
.alert-card .alert-msg { font-size: .85rem; color: var(--text); line-height: 1.5; }
.alert-card .alert-countdown {
  font-size: .75rem; font-weight: 600; padding: 2px 8px; border-radius: 4px;
}
.countdown-active { background: rgba(255,69,58,.15); color: var(--danger); animation: pulse 2s ease-in-out infinite; }
.countdown-upcoming { background: rgba(255,159,10,.15); color: var(--warn); }
.countdown-ended { background: rgba(142,142,147,.15); color: var(--muted); }

.section-toggle {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 10px 0; margin-bottom: 8px; user-select: none;
}
.section-toggle .arrow { transition: transform .2s; font-size: .8rem; }
.section-toggle .arrow.collapsed { transform: rotate(-90deg); }
.section-toggle .section-title { font-weight: 600; font-size: .9rem; }
.section-toggle .section-count {
  font-size: .75rem; color: var(--muted); background: var(--bg);
  padding: 1px 8px; border-radius: 10px; border: 1px solid var(--border);
}
.section-body.collapsed { display: none; }

.empty { text-align: center; color: var(--muted); padding: 40px; font-size: .9rem; }
.empty .empty-icon { font-size: 2rem; margin-bottom: 8px; display: block; }

.history-table { width: 100%; border-collapse: collapse; font-size: .85rem; }
.history-table th {
  text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
  color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .5px;
}
.history-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
.history-table tr:last-child td { border-bottom: none; }

.filter-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }

.chart-wrap { height: 220px; margin-bottom: 16px; }

.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; font-size: .85rem; }
.config-grid .cfg-label { color: var(--muted); }
.config-grid .cfg-value { font-weight: 500; }

.health-row {
  display: flex; align-items: center; gap: 12px; padding: 10px 0;
  border-bottom: 1px solid var(--border); font-size: .85rem;
}
.health-row:last-child { border-bottom: none; }
.health-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.health-dot.ok { background: var(--success); }
.health-dot.err { background: var(--danger); }
.health-dot.unknown { background: var(--muted); }

label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: 4px; }
input[type="date"] {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 6px 10px; border-radius: 8px; font-size: .85rem;
}

@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .5; } }
@keyframes spin { to { transform: rotate(360deg); } }
.spinner {
  width: 14px; height: 14px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite;
  display: inline-block; vertical-align: middle;
}

.hidden { display: none !important; }
</style>
</head>
<body>

<h1><span class="icon">&#9889;</span> Utility Outage Monitor</h1>

<div class="tabs">
  <div class="tab active" data-tab="alerts">Wylaczenia <span class="dot" id="alert-dot"></span></div>
  <div class="tab" data-tab="history">Historia</div>
  <div class="tab" data-tab="status">Status</div>
</div>

<!-- ============================== TAB: ALERTS ============================== -->
<div id="alerts" class="panel active">
  <div class="summary-bar">
    <div class="counts" id="alert-counts"></div>
    <div class="meta" id="last-check-info">Ladowanie...</div>
    <button class="btn btn-sm btn-primary" id="btn-refresh" onclick="doRefresh()">Odswiez</button>
  </div>

  <div id="matched-section">
    <div class="section-toggle" onclick="toggleSection('matched')">
      <span class="arrow" id="matched-arrow">&#9660;</span>
      <span class="section-title">Twoj adres</span>
      <span class="section-count" id="matched-count">0</span>
    </div>
    <div class="section-body" id="matched-body"></div>
  </div>

  <div id="other-section">
    <div class="section-toggle" onclick="toggleSection('other')">
      <span class="arrow collapsed" id="other-arrow">&#9660;</span>
      <span class="section-title">Okolica</span>
      <span class="section-count" id="other-count">0</span>
    </div>
    <div class="section-body collapsed" id="other-body"></div>
  </div>
</div>

<!-- ============================== TAB: HISTORY ============================== -->
<div id="history" class="panel">
  <div class="stats-bar" id="history-stats"></div>

  <div class="card">
    <div class="card-title">Awarie w czasie</div>
    <div class="chart-wrap"><canvas id="history-chart"></canvas></div>
  </div>

  <div class="card">
    <div class="card-title" style="display:flex;justify-content:space-between;align-items:center;">
      Przeszle wylaczenia
      <div class="filter-row" style="margin:0;">
        <button class="btn btn-sm btn-ghost filter-btn active" data-source="">Wszystkie</button>
        <button class="btn btn-sm btn-ghost filter-btn" data-source="tauron">Prad</button>
        <button class="btn btn-sm btn-ghost filter-btn" data-source="mpwik">Woda</button>
      </div>
    </div>
    <div style="max-height:400px;overflow-y:auto;">
      <table class="history-table">
        <thead><tr><th>Data</th><th>Zrodlo</th><th>Czas trwania</th><th>Opis</th></tr></thead>
        <tbody id="history-tbody"></tbody>
      </table>
    </div>
    <div class="empty hidden" id="history-empty">Brak historii wylaczen</div>
  </div>
</div>

<!-- ============================== TAB: STATUS ============================== -->
<div id="status" class="panel">
  <div class="card">
    <div class="card-title">Konfiguracja</div>
    <div class="config-grid" id="config-grid"></div>
  </div>

  <div class="card">
    <div class="card-title">Rozpoznawanie adresu (GAID)</div>
    <div id="gaid-info"></div>
  </div>

  <div class="card">
    <div class="card-title">Stan zrodel danych</div>
    <div id="health-info"></div>
  </div>

  <div class="card">
    <div class="card-title">Nastepne sprawdzenie</div>
    <div id="next-check-info" style="font-size:.9rem;"></div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const BASE = window.location.pathname.replace(/\/+$/, '');
let historyChart = null;
let currentSourceFilter = '';
let pollTimer = null;

// --- Tabs ---
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'history') loadHistory();
    if (t.dataset.tab === 'status') loadStatus();
  });
});

// --- Collapsible sections ---
function toggleSection(name) {
  const body = document.getElementById(name + '-body');
  const arrow = document.getElementById(name + '-arrow');
  body.classList.toggle('collapsed');
  arrow.classList.toggle('collapsed');
}

// --- Time helpers ---
function formatDateTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('pl-PL', {day:'2-digit',month:'2-digit',year:'numeric'}) +
           ' ' + d.toLocaleTimeString('pl-PL', {hour:'2-digit',minute:'2-digit'});
  } catch(e) { return iso; }
}

function getCountdown(startIso, endIso) {
  if (!startIso) return {text: '', cls: ''};
  const now = Date.now();
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : null;

  if (end && now > end) return {text: 'zakonczono', cls: 'countdown-ended'};
  if (now >= start) return {text: 'trwa teraz', cls: 'countdown-active'};

  const diff = start - now;
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(hours / 24);
  if (days > 0) return {text: `za ${days}d ${hours % 24}h`, cls: 'countdown-upcoming'};
  const mins = Math.floor(diff / 60000);
  if (hours > 0) return {text: `za ${hours}h ${mins % 60}m`, cls: 'countdown-upcoming'};
  return {text: `za ${mins}m`, cls: 'countdown-upcoming'};
}

function timeAgo(iso) {
  if (!iso) return 'nigdy';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'przed chwila';
  if (mins < 60) return `${mins} min temu`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins%60}m temu`;
}

function formatDuration(hours) {
  if (hours == null) return '—';
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  return `${hours}h`;
}

// --- Alert rendering ---
function renderAlertCard(alert) {
  const src = alert.source === 'tauron' ? 'tauron' : 'mpwik';
  const srcLabel = src === 'tauron' ? 'Prad' : 'Woda';
  const badgeCls = src === 'tauron' ? 'badge-power' : 'badge-water';
  const cd = getCountdown(alert.start_date, alert.end_date);

  const dates = (alert.start_date || alert.end_date)
    ? `${formatDateTime(alert.start_date)} — ${formatDateTime(alert.end_date)}`
    : '';

  const msg = alert.message || '';
  const desc = alert.description || '';
  const text = desc ? `${msg}\n${desc}` : msg;

  return `<div class="alert-card source-${src}">
    <div class="alert-body">
      <div class="alert-header">
        <span class="badge ${badgeCls}">${srcLabel}</span>
        ${cd.text ? `<span class="alert-countdown ${cd.cls}">${cd.text}</span>` : ''}
      </div>
      ${dates ? `<div class="alert-dates">${dates}</div>` : ''}
      <div class="alert-msg">${escHtml(text)}</div>
    </div>
  </div>`;
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML.replace(/\n/g, '<br>');
}

// --- Load alerts ---
async function loadAlerts() {
  try {
    const [alertsRes, statusRes] = await Promise.all([
      api('/api/alerts'),
      api('/api/status'),
    ]);

    const matched = alertsRes.matched || [];
    const other = alertsRes.other || [];

    document.getElementById('matched-count').textContent = matched.length;
    document.getElementById('other-count').textContent = other.length;

    const dot = document.getElementById('alert-dot');
    if (matched.length > 0) dot.classList.add('visible');
    else dot.classList.remove('visible');

    const countsEl = document.getElementById('alert-counts');
    const tCount = [...matched, ...other].filter(a => a.source === 'tauron').length;
    const wCount = [...matched, ...other].filter(a => a.source === 'mpwik').length;
    countsEl.innerHTML = `
      <span class="badge badge-power">Prad: ${tCount}</span>
      <span class="badge badge-water">Woda: ${wCount}</span>
    `;

    document.getElementById('last-check-info').textContent =
      statusRes.last_check ? `Sprawdzono: ${timeAgo(statusRes.last_check)}` : 'Nie sprawdzono jeszcze';

    const matchedBody = document.getElementById('matched-body');
    if (matched.length) {
      matchedBody.innerHTML = matched.map(renderAlertCard).join('');
    } else {
      matchedBody.innerHTML = '<div class="empty"><span class="empty-icon">&#10003;</span>Brak aktywnych wylaczen dla Twojego adresu</div>';
    }

    const otherBody = document.getElementById('other-body');
    if (other.length) {
      otherBody.innerHTML = other.map(renderAlertCard).join('');
    } else {
      otherBody.innerHTML = '<div class="empty">Brak wylaczen w okolicy</div>';
    }

  } catch(e) { console.error('loadAlerts', e); }
}

async function doRefresh() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Sprawdzam...';
  await api('/api/refresh', 'POST');
  setTimeout(async () => {
    await loadAlerts();
    btn.disabled = false;
    btn.textContent = 'Odswiez';
  }, 3000);
}

// --- Load history ---
async function loadHistory() {
  try {
    const [statsRes, histRes] = await Promise.all([
      api('/api/history/stats'),
      api('/api/history?source=' + currentSourceFilter),
    ]);

    const statsEl = document.getElementById('history-stats');
    statsEl.innerHTML = `
      <div class="stat">
        <div class="stat-val" style="color:var(--accent)">${statsRes.this_month_count}</div>
        <div class="stat-lbl">Ten miesiac</div>
      </div>
      <div class="stat">
        <div class="stat-val">${formatDuration(statsRes.avg_duration_hours)}</div>
        <div class="stat-lbl">Sredni czas</div>
      </div>
      <div class="stat">
        <div class="stat-val" style="color:var(--warn)">${formatDuration(statsRes.longest_hours)}</div>
        <div class="stat-lbl">Najdluzsze</div>
      </div>
      <div class="stat">
        <div class="stat-val" style="color:var(--success)">${statsRes.days_since_last ?? '—'}</div>
        <div class="stat-lbl">Dni od ostatniego</div>
      </div>
    `;

    renderChart(statsRes.chart);

    const tbody = document.getElementById('history-tbody');
    const emptyEl = document.getElementById('history-empty');
    const rows = histRes.history || [];

    if (rows.length === 0) {
      tbody.innerHTML = '';
      emptyEl.classList.remove('hidden');
    } else {
      emptyEl.classList.add('hidden');
      tbody.innerHTML = rows.slice(0, 100).map(h => {
        const src = h.source === 'tauron' ? 'tauron' : 'mpwik';
        const srcLabel = src === 'tauron' ? 'Prad' : 'Woda';
        const badgeCls = src === 'tauron' ? 'badge-power' : 'badge-water';
        const msg = (h.message || '').substring(0, 80);
        return `<tr>
          <td>${formatDateTime(h.start_date)}</td>
          <td><span class="badge ${badgeCls}">${srcLabel}</span></td>
          <td>${formatDuration(h.duration_hours)}</td>
          <td style="color:var(--muted);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(msg)}</td>
        </tr>`;
      }).join('');
    }
  } catch(e) { console.error('loadHistory', e); }
}

function renderChart(data) {
  if (!data || !data.labels) return;
  const ctx = document.getElementById('history-chart');
  if (!ctx) return;

  if (historyChart) historyChart.destroy();

  historyChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.labels,
      datasets: [
        {
          label: 'Prad',
          data: data.tauron,
          backgroundColor: 'rgba(255,159,10,0.7)',
          borderRadius: 4,
        },
        {
          label: 'Woda',
          data: data.mpwik,
          backgroundColor: 'rgba(50,173,230,0.7)',
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8e8e93', font: { size: 11 } } },
      },
      scales: {
        x: {
          stacked: true,
          ticks: { color: '#8e8e93', font: { size: 11 } },
          grid: { color: 'rgba(58,58,60,0.5)' },
        },
        y: {
          stacked: true,
          beginAtZero: true,
          ticks: { color: '#8e8e93', stepSize: 1, font: { size: 11 } },
          grid: { color: 'rgba(58,58,60,0.5)' },
        },
      },
    },
  });
}

document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentSourceFilter = btn.dataset.source;
    loadHistory();
  });
});

// --- Load status ---
async function loadStatus() {
  try {
    const res = await api('/api/status');
    const cfg = res.config || {};

    document.getElementById('config-grid').innerHTML = `
      <div class="cfg-label">Miasto</div><div class="cfg-value">${cfg.city_name || '—'}</div>
      <div class="cfg-label">Ulica</div><div class="cfg-value">${cfg.street_name || '—'}</div>
      <div class="cfg-label">Numer</div><div class="cfg-value">${cfg.house_number || '—'}</div>
      <div class="cfg-label">Interwat sprawdzania</div><div class="cfg-value">${cfg.check_interval} min</div>
      <div class="cfg-label">Tauron (prad)</div><div class="cfg-value">${cfg.enable_tauron ? 'Wlaczony' : 'Wylaczony'}</div>
      <div class="cfg-label">MPWiK (woda)</div><div class="cfg-value">${cfg.enable_mpwik ? 'Wlaczony' : 'Wylaczony'}</div>
      <div class="cfg-label">Push na telefon</div><div class="cfg-value">${cfg.notify_mobile ? 'Tak' : 'Nie'}</div>
      <div class="cfg-label">Urzadzenia</div><div class="cfg-value">${(cfg.mobile_notify_services || []).join(', ') || '—'}</div>
      <div class="cfg-label">Powiadomienia HA</div><div class="cfg-value">${cfg.notify_persistent ? 'Tak' : 'Nie'}</div>
    `;

    const gaid = res.gaid || {};
    const gaidStatus = gaid.status || 'pending';
    const gaidBadge = gaidStatus === 'resolved' ? 'badge-ok' :
                      gaidStatus === 'error' ? 'badge-error' : 'badge-pending';
    document.getElementById('gaid-info').innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="badge ${gaidBadge}">${gaidStatus}</span>
      </div>
      <div style="font-size:.85rem;color:var(--muted)">
        City GAID: <strong style="color:var(--text)">${gaid.city || '—'}</strong> &nbsp;
        Street GAID: <strong style="color:var(--text)">${gaid.street || '—'}</strong>
      </div>
    `;

    const health = res.api_health || {};
    document.getElementById('health-info').innerHTML = ['tauron', 'mpwik'].map(src => {
      const h = health[src] || {};
      const ok = h.last_ok && !h.last_error;
      const dotCls = h.last_ok ? (h.last_error ? 'err' : 'ok') : 'unknown';
      const label = src === 'tauron' ? 'Tauron (prad)' : 'MPWiK (woda)';
      return `<div class="health-row">
        <div class="health-dot ${dotCls}"></div>
        <div style="flex:1"><strong>${label}</strong></div>
        <div style="color:var(--muted);font-size:.8rem;">
          ${h.last_ok ? timeAgo(h.last_ok) : 'brak danych'}
          ${h.response_ms != null ? `(${h.response_ms}ms)` : ''}
        </div>
        ${h.last_error ? `<div style="color:var(--danger);font-size:.8rem;width:100%;margin-top:4px">${escHtml(h.last_error)}</div>` : ''}
      </div>`;
    }).join('');

    document.getElementById('next-check-info').innerHTML = res.check_running
      ? '<span class="spinner"></span> Sprawdzanie w toku...'
      : `Nastepne sprawdzenie: <strong>${res.last_check ? 'za ok. ' + cfg.check_interval + ' min' : 'wkrotce'}</strong>`;

  } catch(e) { console.error('loadStatus', e); }
}

// --- Helpers ---
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

// --- Auto-refresh ---
function startAutoRefresh() {
  loadAlerts();
  setInterval(loadAlerts, 60000);
}

startAutoRefresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _startup():
    init_state()
    resolve_gaid()
    run_check()
    scheduler.add_job(run_check, "interval", minutes=CHECK_INTERVAL, id="outage_check")
    scheduler.start()


_startup_thread = threading.Thread(target=_startup, daemon=True)
_startup_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("INGRESS_PORT", 8099))
    app.run(host="0.0.0.0", port=port)
