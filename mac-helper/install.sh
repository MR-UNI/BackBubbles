#!/usr/bin/env bash
# BackBubbles Mac Helper — installer
#
# Creates a Python virtual-env, installs Playwright + Chromium, writes a
# LaunchAgent plist so the helper auto-starts at login, and loads it.
#
# Usage:
#   bash install.sh
#
# Optional variables:
#   BB_GMESSAGES_PROFILE_DIR   (default: ~/.backbubbles/gmessages-profile)
#   BB_GMESSAGES_POLL_INTERVAL (default: 3.0)
#   BB_POLL_INTERVAL           (default: 2.0)
#
# After installing, run the pairing step once to authenticate:
#   source .venv/bin/activate && python main.py --pair

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

PROFILE_DIR="${BB_GMESSAGES_PROFILE_DIR:-${HOME}/.backbubbles/gmessages-profile}"
POLL_INTERVAL="${BB_POLL_INTERVAL:-2.0}"
GMESSAGES_POLL_INTERVAL="${BB_GMESSAGES_POLL_INTERVAL:-3.0}"

# ---------------------------------------------------------------------------
# Python venv + dependencies
# ---------------------------------------------------------------------------
echo "→ Creating Python virtual environment in ${VENV_DIR} …"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
echo "  ✓ Python dependencies installed."

# ---------------------------------------------------------------------------
# Playwright browser
# ---------------------------------------------------------------------------
echo "→ Installing Playwright Chromium browser …"
"${VENV_DIR}/bin/playwright" install chromium
echo "  ✓ Chromium installed."

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
    -e "s|GMESSAGES_PROFILE_DIR_PLACEHOLDER|${PROFILE_DIR}|g" \
    -e "s|LOG_DIR_PLACEHOLDER|${LOG_DIR}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_DEST}"

# Patch optional env vars directly via PlistBuddy.
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:BB_POLL_INTERVAL ${POLL_INTERVAL}" "${PLIST_DEST}"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:BB_GMESSAGES_POLL_INTERVAL ${GMESSAGES_POLL_INTERVAL}" "${PLIST_DEST}"

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
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  NEXT STEP: Pair your phone with Google Messages for Web     ║"
echo "║                                                              ║"
echo "║  Run the following command to open the pairing window:       ║"
echo "║                                                              ║"
echo "║    cd ${SCRIPT_DIR}"
echo "║    source .venv/bin/activate"
echo "║    python main.py --pair                                     ║"
echo "║                                                              ║"
echo "║  Scan the QR code in Google Messages on your Android phone.  ║"
echo "║  After pairing, BackBubbles will restart automatically in    ║"
echo "║  headless mode.                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Logs: ${LOG_DIR}/backbubbles.log"
echo "      ${LOG_DIR}/backbubbles.error.log"
echo ""
echo "To stop:    launchctl unload ${PLIST_DEST}"
echo "To restart: launchctl unload ${PLIST_DEST} && launchctl load ${PLIST_DEST}"
echo "To re-pair: source ${VENV_DIR}/bin/activate && python ${SCRIPT_DIR}/main.py --pair"
