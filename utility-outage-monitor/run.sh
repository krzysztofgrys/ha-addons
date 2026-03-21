#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/options.json"

if [ ! -f "$CONFIG" ]; then
    echo "[ERROR] Configuration file not found: $CONFIG"
    exit 1
fi

export CITY_NAME="$(jq -r '.city_name // "Wroclaw"' "$CONFIG")"
export STREET_NAME="$(jq -r '.street_name // ""' "$CONFIG")"
export HOUSE_NUMBER="$(jq -r '.house_number // ""' "$CONFIG")"
export CHECK_INTERVAL="$(jq -r '.check_interval // 30' "$CONFIG")"
export ENABLE_TAURON="$(jq -r '.enable_tauron // true' "$CONFIG")"
export ENABLE_MPWIK="$(jq -r '.enable_mpwik // true' "$CONFIG")"
export NOTIFY_MOBILE="$(jq -r '.notify_mobile // true' "$CONFIG")"
export MOBILE_NOTIFY_SERVICES="$(jq -r '.mobile_notify_services // "mobile_app_phone"' "$CONFIG")"
export NOTIFY_PERSISTENT="$(jq -r '.notify_persistent // true' "$CONFIG")"

echo "[INFO] Utility Outage Monitor"
echo "[INFO]   City        : ${CITY_NAME}"
echo "[INFO]   Street      : ${STREET_NAME}"
echo "[INFO]   House       : ${HOUSE_NUMBER}"
echo "[INFO]   Interval    : ${CHECK_INTERVAL} min"
echo "[INFO]   Tauron      : ${ENABLE_TAURON}"
echo "[INFO]   MPWiK       : ${ENABLE_MPWIK}"
echo "[INFO]   Mobile push : ${NOTIFY_MOBILE}"
echo "[INFO]   Services    : ${MOBILE_NOTIFY_SERVICES}"

exec python /app.py
