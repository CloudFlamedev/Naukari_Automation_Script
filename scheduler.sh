#!/usr/bin/env bash
# scheduler.sh — Install or remove cron jobs for Naukri Skill Updater.
#
# Usage:
#   bash scheduler.sh install   # Add morning + evening cron jobs
#   bash scheduler.sh remove    # Remove the cron jobs
#   bash scheduler.sh status    # Show current cron jobs for this script
#
# Runs at 9:00 AM IST and 7:00 PM IST.
# IST = UTC+5:30, so:
#   9:00 AM IST = 03:30 UTC
#   7:00 PM IST = 13:30 UTC

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python"
MAIN="${SCRIPT_DIR}/main.py"
LOG_CRON="${SCRIPT_DIR}/logs/cron.log"
CRON_TAG="# naukri-skill-updater"

# Use xvfb-run to provide an in-memory virtual display if available (avoids bot detection),
# or fall back to --headless if xvfb-run is not installed.
if command -v xvfb-run >/dev/null 2>&1; then
  CMD_RUNNER="xvfb-run -a ${VENV_PYTHON} ${MAIN}"
else
  CMD_RUNNER="${VENV_PYTHON} ${MAIN} --headless"
fi

MORNING_CRON="0 9 * * * export TZ=Asia/Kolkata; ${CMD_RUNNER} morning >> ${LOG_CRON} 2>&1 ${CRON_TAG}"
EVENING_CRON="0 19 * * * export TZ=Asia/Kolkata; ${CMD_RUNNER} evening >> ${LOG_CRON} 2>&1 ${CRON_TAG}"

cmd="${1:-status}"

case "$cmd" in
  install)
    # Remove any existing entries to avoid duplicates.
    (crontab -l 2>/dev/null || true) \
      | grep -v "${CRON_TAG}" \
      | { cat; echo "${MORNING_CRON}"; echo "${EVENING_CRON}"; } \
      | crontab -
    echo "✅  Cron jobs installed."
    echo "    Morning : 9:00 AM IST (09:00) → ${CMD_RUNNER} morning"
    echo "    Evening : 7:00 PM IST (19:00) → ${CMD_RUNNER} evening"
    echo "    Cron log: ${LOG_CRON}"
    ;;

  remove)
    (crontab -l 2>/dev/null || true) \
      | grep -v "${CRON_TAG}" \
      | crontab -
    echo "✅  Cron jobs removed."
    ;;

  status)
    echo "Current naukri-skill-updater cron jobs:"
    (crontab -l 2>/dev/null || true) | grep "${CRON_TAG}" || echo "  (none installed)"
    ;;

  *)
    echo "Usage: $0 {install|remove|status}" >&2
    exit 1
    ;;
esac
