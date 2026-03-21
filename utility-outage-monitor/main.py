"""Utility Outage Monitor -- backend logic.

Polls Tauron Dystrybucja (power) and MPWiK Wroclaw (water) public APIs,
matches alerts to the user's address, persists history, and dispatches
Home Assistant notifications.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("outage-monitor")

# ---------------------------------------------------------------------------
# Configuration from environment (set by run.sh)
# ---------------------------------------------------------------------------

CITY_NAME = os.environ.get("CITY_NAME", "Wroclaw")
STREET_NAME = os.environ.get("STREET_NAME", "")
HOUSE_NUMBER = os.environ.get("HOUSE_NUMBER", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))
ENABLE_TAURON = os.environ.get("ENABLE_TAURON", "true").lower() == "true"
ENABLE_MPWIK = os.environ.get("ENABLE_MPWIK", "true").lower() == "true"
NOTIFY_MOBILE = os.environ.get("NOTIFY_MOBILE", "true").lower() == "true"
MOBILE_NOTIFY_SERVICES = [
    s.strip()
    for s in os.environ.get("MOBILE_NOTIFY_SERVICES", "mobile_app_phone").split(",")
    if s.strip()
]
NOTIFY_PERSISTENT = os.environ.get("NOTIFY_PERSISTENT", "true").lower() == "true"

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DATA_DIR = Path("/data")
ALERTS_FILE = DATA_DIR / "alerts.json"
HISTORY_FILE = DATA_DIR / "history.json"
GAID_CACHE_FILE = DATA_DIR / "gaid_cache.json"

HISTORY_MAX_ENTRIES = 1000

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "alerts": [],
    "last_check": None,
    "last_check_ok": {},
    "next_check": None,
    "gaid": {"city": None, "street": None, "status": "pending"},
    "api_health": {
        "tauron": {"last_ok": None, "last_error": None, "response_ms": None},
        "mpwik": {"last_ok": None, "last_error": None, "response_ms": None},
    },
    "check_running": False,
}


def get_state():
    with _lock:
        return json.loads(json.dumps(_state, default=str))


def get_alerts():
    with _lock:
        return list(_state["alerts"])


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path, default=None):
    if default is None:
        default = []
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to load %s: %s", path, exc)
    return default


def _save_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        log.error("Failed to save %s: %s", path, exc)


def load_history():
    return _load_json(HISTORY_FILE, [])


def save_history(history: list):
    if len(history) > HISTORY_MAX_ENTRIES:
        history = history[-HISTORY_MAX_ENTRIES:]
    _save_json(HISTORY_FILE, history)


def _load_alerts():
    return _load_json(ALERTS_FILE, [])


def _save_alerts(alerts: list):
    _save_json(ALERTS_FILE, alerts)


# ---------------------------------------------------------------------------
# Alert ID generation
# ---------------------------------------------------------------------------

def _alert_id(source: str, start_date: str, message: str) -> str:
    raw = f"{source}|{start_date or ''}|{message or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Tauron Dystrybucja API client
# ---------------------------------------------------------------------------

TAURON_BASE = "https://www.tauron-dystrybucja.pl/waapi"
TAURON_HEADERS = {
    "accept": "application/json",
    "x-requested-with": "XMLHttpRequest",
    "Referer": "https://www.tauron-dystrybucja.pl/wylaczenia",
}


def tauron_lookup_city(name: str) -> dict | None:
    ts = int(time.time() * 1000)
    url = f"{TAURON_BASE}/enum/geo/cities"
    resp = requests.get(url, params={"partName": name, "_": ts}, headers=TAURON_HEADERS, timeout=15)
    resp.raise_for_status()
    items = resp.json()
    if not items:
        return None
    for item in items:
        if item.get("Name", "").lower() == name.lower():
            return item
    return items[0]


def tauron_lookup_street(name: str, city_gaid: int) -> dict | None:
    ts = int(time.time() * 1000)
    url = f"{TAURON_BASE}/enum/geo/streets"
    resp = requests.get(
        url,
        params={"partName": name, "ownerGAID": city_gaid, "_": ts},
        headers=TAURON_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        return None
    name_lower = name.lower()
    for item in items:
        if item.get("Name", "").lower() == name_lower:
            return item
    return items[0]


def tauron_fetch_outages(city_gaid: int, street_gaid: int, house_no: str) -> list[dict]:
    ts = int(time.time() * 1000)
    from_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    url = f"{TAURON_BASE}/outages/address"
    resp = requests.get(
        url,
        params={
            "cityGAID": city_gaid,
            "streetGAID": street_gaid,
            "houseNo": house_no,
            "fromDate": from_date,
            "getLightingSupport": "false",
            "getServicedSwitchingoff": "true",
            "_": ts,
        },
        headers=TAURON_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    outages = []
    for item in data.get("OutageItems", []):
        outages.append({
            "source": "tauron",
            "start_date": item.get("StartDate"),
            "end_date": item.get("EndDate"),
            "message": item.get("Message", ""),
            "description": item.get("Description", ""),
        })
    return outages


# ---------------------------------------------------------------------------
# MPWiK Wroclaw API client
# ---------------------------------------------------------------------------

MPWIK_URL = "https://www.mpwik.wroc.pl/wp-admin/admin-ajax.php"
MPWIK_HEADERS = {
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "accept": "application/json",
    "x-requested-with": "XMLHttpRequest",
    "origin": "https://www.mpwik.wroc.pl",
    "referer": "https://www.mpwik.wroc.pl/",
}


def _parse_mpwik_date(raw: str | None) -> str | None:
    """Convert 'DD-MM-YYYY HH:mm' to ISO 8601."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d-%m-%Y %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:00")
        except ValueError:
            continue
    return raw


def mpwik_fetch_failures() -> list[dict]:
    resp = requests.post(MPWIK_URL, data="action=all", headers=MPWIK_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    failures = []
    for item in data.get("failures", []):
        failures.append({
            "source": "mpwik",
            "start_date": _parse_mpwik_date(item.get("date_start")),
            "end_date": _parse_mpwik_date(item.get("date_end")),
            "message": (item.get("content") or "").strip(),
            "description": "",
        })
    return failures


# ---------------------------------------------------------------------------
# Address matching
# ---------------------------------------------------------------------------

_STREET_PREFIX_RE = re.compile(
    r"^(ul\.\s*|al\.\s*|pl\.\s*|os\.\s*|rondo\s+)", re.IGNORECASE
)


def _normalize_street(name: str) -> str:
    return _STREET_PREFIX_RE.sub("", name).strip()


def matches_address(alert: dict, street: str) -> bool:
    """Check if alert message/description mentions the given street."""
    if not street:
        return False
    normalized = _normalize_street(street)
    text = f"{alert.get('message', '')} {alert.get('description', '')}".lower()

    if normalized.lower() in text:
        return True

    words = [w for w in normalized.split() if len(w) >= 3]
    for word in words:
        try:
            if re.search(r"\b" + re.escape(word.lower()) + r"\b", text):
                return True
        except re.error:
            if word.lower() in text:
                return True

    return False


# ---------------------------------------------------------------------------
# GAID resolution
# ---------------------------------------------------------------------------

def resolve_gaid(force: bool = False) -> bool:
    cache = _load_json(GAID_CACHE_FILE, {})

    if (
        not force
        and cache.get("city_name") == CITY_NAME
        and cache.get("street_name") == STREET_NAME
        and cache.get("city_gaid")
        and cache.get("street_gaid")
    ):
        with _lock:
            _state["gaid"] = {
                "city": cache["city_gaid"],
                "street": cache["street_gaid"],
                "status": "resolved",
            }
        log.info("GAID loaded from cache: city=%s street=%s", cache["city_gaid"], cache["street_gaid"])
        return True

    try:
        city = tauron_lookup_city(CITY_NAME)
        if not city:
            log.error("Could not resolve city GAID for '%s'", CITY_NAME)
            with _lock:
                _state["gaid"]["status"] = "error"
            return False

        street = tauron_lookup_street(STREET_NAME, city["GAID"])
        if not street:
            log.error("Could not resolve street GAID for '%s' in city %s", STREET_NAME, city["GAID"])
            with _lock:
                _state["gaid"]["status"] = "error"
            return False

        cache_data = {
            "city_name": CITY_NAME,
            "street_name": STREET_NAME,
            "city_gaid": city["GAID"],
            "street_gaid": street["GAID"],
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_json(GAID_CACHE_FILE, cache_data)

        with _lock:
            _state["gaid"] = {
                "city": city["GAID"],
                "street": street["GAID"],
                "status": "resolved",
            }

        log.info("GAID resolved: city=%s (%s) street=%s (%s)",
                 city["Name"], city["GAID"], street["Name"], street["GAID"])
        return True

    except Exception as exc:
        log.error("GAID resolution failed: %s", exc)
        with _lock:
            _state["gaid"]["status"] = "error"
        return False


# ---------------------------------------------------------------------------
# Home Assistant notifications
# ---------------------------------------------------------------------------

HA_PERSISTENT_URL = "http://supervisor/core/api/services/persistent_notification/create"
HA_NOTIFY_URL_TPL = "http://supervisor/core/api/services/notify/{service}"


def _ha_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def send_persistent_notification(title: str, message: str, notification_id: str | None = None):
    if not SUPERVISOR_TOKEN or not NOTIFY_PERSISTENT:
        return
    payload = {"title": title, "message": message}
    if notification_id:
        payload["notification_id"] = notification_id
    try:
        resp = requests.post(HA_PERSISTENT_URL, json=payload, headers=_ha_headers(), timeout=10)
        if resp.status_code < 300:
            log.info("Persistent notification sent: %s", title)
        else:
            log.warning("Persistent notification failed (%s): %s", resp.status_code, resp.text)
    except Exception as exc:
        log.error("Persistent notification error: %s", exc)


def send_mobile_notification(title: str, message: str):
    if not SUPERVISOR_TOKEN or not NOTIFY_MOBILE:
        return
    for service in MOBILE_NOTIFY_SERVICES:
        url = HA_NOTIFY_URL_TPL.format(service=service)
        payload = {"title": title, "message": message}
        try:
            resp = requests.post(url, json=payload, headers=_ha_headers(), timeout=10)
            if resp.status_code < 300:
                log.info("Mobile notification sent to %s: %s", service, title)
            else:
                log.warning("Mobile notify %s failed (%s): %s", service, resp.status_code, resp.text)
        except Exception as exc:
            log.error("Mobile notify %s error: %s", service, exc)


def _notify_new_alert(alert: dict):
    source_label = "Prad" if alert["source"] == "tauron" else "Woda"
    title = f"Wylaczenie: {source_label}"

    start = alert.get("start_date", "?")
    end = alert.get("end_date", "?")
    msg = alert.get("message", "")
    desc = alert.get("description", "")

    body_parts = [f"{STREET_NAME} {HOUSE_NUMBER}".strip()]
    if start or end:
        body_parts.append(f"{start} — {end}")
    if msg:
        body_parts.append(msg[:200])
    if desc:
        body_parts.append(desc[:200])
    body = "\n".join(body_parts)

    nid = f"outage_{alert.get('id', '')}"
    send_persistent_notification(title, body, notification_id=nid)
    send_mobile_notification(title, body)


# ---------------------------------------------------------------------------
# Core check cycle
# ---------------------------------------------------------------------------

def run_check():
    with _lock:
        if _state["check_running"]:
            log.info("Check already running, skipping")
            return
        _state["check_running"] = True

    log.info("Starting outage check...")
    now_iso = datetime.now(timezone.utc).isoformat()

    all_alerts: list[dict] = []
    futures = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        if ENABLE_TAURON:
            gaid = None
            with _lock:
                gaid = _state["gaid"]
            if gaid.get("status") != "resolved":
                resolve_gaid()
                with _lock:
                    gaid = _state["gaid"]
            if gaid.get("city") and gaid.get("street"):
                futures["tauron"] = pool.submit(
                    tauron_fetch_outages, gaid["city"], gaid["street"], HOUSE_NUMBER
                )

        if ENABLE_MPWIK:
            futures["mpwik"] = pool.submit(mpwik_fetch_failures)

        for source, future in futures.items():
            t0 = time.time()
            try:
                results = future.result(timeout=30)
                elapsed = int((time.time() - t0) * 1000)
                all_alerts.extend(results)
                with _lock:
                    _state["api_health"][source]["last_ok"] = now_iso
                    _state["api_health"][source]["last_error"] = None
                    _state["api_health"][source]["response_ms"] = elapsed
                log.info("Fetched %d alerts from %s (%dms)", len(results), source, elapsed)
            except Exception as exc:
                elapsed = int((time.time() - t0) * 1000)
                with _lock:
                    _state["api_health"][source]["last_error"] = str(exc)
                    _state["api_health"][source]["response_ms"] = elapsed
                log.error("Failed to fetch %s: %s", source, exc)

    now = datetime.now(timezone.utc)
    active_alerts = []
    for alert in all_alerts:
        end_str = alert.get("end_date")
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if end_dt < now:
                    continue
            except (ValueError, TypeError):
                pass

        alert["id"] = _alert_id(alert["source"], alert.get("start_date", ""), alert.get("message", ""))
        alert["matched"] = (
            alert["source"] == "tauron"
            or matches_address(alert, STREET_NAME)
        )
        active_alerts.append(alert)

    previous_ids = {a["id"] for a in _load_alerts()}
    new_alerts = [a for a in active_alerts if a["id"] not in previous_ids and a.get("matched")]

    for alert in new_alerts:
        _notify_new_alert(alert)

    _save_alerts(active_alerts)

    _archive_expired(previous_ids, {a["id"] for a in active_alerts})

    with _lock:
        _state["alerts"] = active_alerts
        _state["last_check"] = now_iso
        _state["check_running"] = False

    log.info("Check complete: %d active alerts, %d new notifications sent",
             len(active_alerts), len(new_alerts))


def _archive_expired(old_ids: set, current_ids: set):
    """Move alerts that disappeared from active to history."""
    gone_ids = old_ids - current_ids
    if not gone_ids:
        return

    old_alerts = _load_alerts()
    history = load_history()
    now_iso = datetime.now(timezone.utc).isoformat()

    for alert in old_alerts:
        if alert.get("id") in gone_ids:
            duration = None
            start = alert.get("start_date")
            end = alert.get("end_date")
            if start and end:
                try:
                    s = datetime.fromisoformat(start)
                    e = datetime.fromisoformat(end)
                    duration = round((e - s).total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    pass

            history.append({
                **alert,
                "resolved_at": now_iso,
                "duration_hours": duration,
            })

    save_history(history)


def init_state():
    """Load persisted alerts into memory on startup."""
    alerts = _load_alerts()
    with _lock:
        _state["alerts"] = alerts
    log.info("Loaded %d persisted alerts", len(alerts))
