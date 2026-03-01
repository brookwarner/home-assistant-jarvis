#!/bin/sh
# Jarvis start script
set -e

JARVIS_DIR=/homeassistant/jarvis
PYTHON=$JARVIS_DIR/.venv/bin/python

# Ensure Python is available (SSH addon update may have wiped /usr/bin/python3)
if ! "$PYTHON" --version >/dev/null 2>&1; then
    echo "Python missing, installing via apk..."
    apk add --quiet python3 python3-dev py3-pip 2>&1 || true
fi

# Exit early if already running (makes this safe to call from watchdog every 5 min)
if [ -f "$JARVIS_DIR/jarvis.pid" ]; then
    OLD_PID=$(cat "$JARVIS_DIR/jarvis.pid")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Jarvis already running (pid $OLD_PID)"
        exit 0
    fi
fi

# Free up port 8765 if something else grabbed it
fuser -k 8765/tcp 2>/dev/null || true

echo "Starting Jarvis..."
PYTHONPATH=/homeassistant nohup "$PYTHON" "$JARVIS_DIR/bot.py" >> "$JARVIS_DIR/jarvis.log" 2>&1 &
echo $! > "$JARVIS_DIR/jarvis.pid"
echo "Jarvis started (pid $!)"
