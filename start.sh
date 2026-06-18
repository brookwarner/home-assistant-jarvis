#!/bin/sh
# start.sh — idempotent launcher for Jarvis (bare-metal / Home Assistant OS).
#
# Safe to call repeatedly: it exits immediately if Jarvis is already running and
# starts it otherwise. That makes it usable directly from a Home Assistant
# `shell_command` on a watchdog (e.g. every 5 minutes) as well as by hand.
set -e

# Resolve the directory this script lives in, so it works wherever the repo is
# cloned (/homeassistant/jarvis, /config/jarvis, ...). The package imports as
# `jarvis`, so the repo folder must be named `jarvis` and its PARENT must be on
# PYTHONPATH (e.g. PYTHONPATH=/homeassistant for /homeassistant/jarvis).
DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT="$(dirname "$DIR")"
cd "$DIR"

PID_FILE="$DIR/jarvis.pid"
LOG_FILE="$DIR/jarvis.log"
PYTHON="$DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# Already running? Bail out quietly so the watchdog is a no-op.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

# Stale pid file from a crashed process — clean it up before relaunching.
[ -f "$PID_FILE" ] && rm -f "$PID_FILE"

PYTHONPATH="$PARENT" nohup "$PYTHON" "$DIR/bot.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "Jarvis started (pid $(cat "$PID_FILE")). Logs: $LOG_FILE"
