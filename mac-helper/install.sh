#!/usr/bin/env bash
# BackBubbles Mac Helper — installer
#
# Creates a Python virtual-env, installs dependencies, writes a
# LaunchAgent plist so the helper auto-starts at login, and loads it.
#
# Usage:
#   BB_ANDROID_HOST=192.168.1.42 bash install.sh
#
# Optional variables:
#   BB_ANDROID_PORT    (default: 8765)
#   BB_POLL_INTERVAL   (default: 2.0)
#   BB_RECONNECT_DELAY (default: 5.0)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
LABEL="com.backbubbles.helper"
PLIST_TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/BackBubbles"

ANDROID_HOST="${BB_ANDROID_HOST:-}"
ANDROID_PORT="${BB_ANDROID_PORT:-8765}"
POLL_INTERVAL="${BB_POLL_INTERVAL:-2.0}"
RECONNECT_DELAY="${BB_RECONNECT_DELAY:-5.0}"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ -z "${ANDROID_HOST}" ]]; then
    echo "ERROR: BB_ANDROID_HOST is not set."
    echo "  Usage: BB_ANDROID_HOST=<android-ip> bash install.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# Python venv
# ---------------------------------------------------------------------------
echo "→ Creating Python virtual environment in ${VENV_DIR} …"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
echo "  ✓ Dependencies installed."

# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
mkdir -p "${LOG_DIR}"

# ---------------------------------------------------------------------------
# Write LaunchAgent plist
# ---------------------------------------------------------------------------
mkdir -p "${LAUNCH_AGENTS_DIR}"
sed \
    -e "s|VENV_PYTHON_PLACEHOLDER|${VENV_DIR}/bin/python3|g" \
    -e "s|MAIN_PY_PLACEHOLDER|${SCRIPT_DIR}/main.py|g" \
    -e "s|ANDROID_HOST_PLACEHOLDER|${ANDROID_HOST}|g" \
    -e "s|LOG_DIR_PLACEHOLDER|${LOG_DIR}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_DEST}"

# Patch optional env vars directly via PlistBuddy.
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:BB_ANDROID_PORT ${ANDROID_PORT}" "${PLIST_DEST}"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:BB_POLL_INTERVAL ${POLL_INTERVAL}" "${PLIST_DEST}"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:BB_RECONNECT_DELAY ${RECONNECT_DELAY}" "${PLIST_DEST}"

echo "  ✓ LaunchAgent plist written to ${PLIST_DEST}"

# ---------------------------------------------------------------------------
# Load (or reload) the LaunchAgent
# ---------------------------------------------------------------------------
if launchctl list "${LABEL}" &>/dev/null; then
    echo "→ Reloading existing LaunchAgent …"
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
fi

launchctl load "${PLIST_DEST}"
echo "  ✓ LaunchAgent loaded — BackBubbles will start at login."
echo ""
echo "Logs: ${LOG_DIR}/backbubbles.log"
echo "      ${LOG_DIR}/backbubbles.error.log"
echo ""
echo "To stop:    launchctl unload ${PLIST_DEST}"
echo "To restart: launchctl unload ${PLIST_DEST} && launchctl load ${PLIST_DEST}"
