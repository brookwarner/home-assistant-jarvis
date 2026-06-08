#!/bin/sh
# Jarvis start script
set -e

JARVIS_DIR=/homeassistant/jarvis
PYTHON=$JARVIS_DIR/.venv/bin/python

# Jarvis runs in the SSH/terminal add-on container, where /homeassistant/jarvis and its
# venv exist. HA Core's shell_command (the 5-min watchdog) runs in a DIFFERENT container
# without that path or venv. Exit cleanly there so the watchdog is a harmless no-op
# instead of running apk and spawning broken processes every 5 minutes.
if [ ! -d "$JARVIS_DIR" ] || [ ! -x "$PYTHON" ]; then
    echo "$JARVIS_DIR/.venv not present in this container; nothing to do"
    exit 0
fi

# Ensure Python is available (SSH addon update may have wiped /usr/bin/python3)
if ! "$PYTHON" --version >/dev/null 2>&1; then
    echo "Python missing, installing via apk..."
    apk add --quiet python3 python3-dev py3-pip 2>&1 || true
fi

# Exit early if already running (makes this safe to call from watchdog every 5 min),
# UNLESS a force-restart sentinel is present — then kill the old process and restart.
if [ -f "$JARVIS_DIR/jarvis.pid" ]; then
    OLD_PID=$(cat "$JARVIS_DIR/jarvis.pid")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        if [ -f "$JARVIS_DIR/.force_restart" ]; then
            echo "Force restart: killing pid $OLD_PID"
            kill "$OLD_PID" 2>/dev/null || true
            rm -f "$JARVIS_DIR/.force_restart"
            sleep 3
        else
            echo "Jarvis already running (pid $OLD_PID)"
            exit 0
        fi
    fi
fi

# Free up port 8765 if something else grabbed it (fuser not available in HA Alpine)
kill "$(lsof -ti:8765 2>/dev/null)" 2>/dev/null || pkill -f "bot.py" 2>/dev/null || true

echo "Starting Jarvis..."
PYTHONPATH=/homeassistant nohup "$PYTHON" "$JARVIS_DIR/bot.py" >> "$JARVIS_DIR/jarvis.log" 2>&1 &
echo $! > "$JARVIS_DIR/jarvis.pid"
echo "Jarvis started (pid $!)"
