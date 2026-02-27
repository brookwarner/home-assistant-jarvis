#!/bin/bash
# Jarvis start script
set -e

JARVIS_DIR=/homeassistant/jarvis
PYTHON=$JARVIS_DIR/.venv/bin/python

# Kill any existing instance holding the webhook port or PID file
if [ -f "$JARVIS_DIR/jarvis.pid" ]; then
    OLD_PID=$(cat "$JARVIS_DIR/jarvis.pid")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing Jarvis (pid $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
fi

# Also free up port 8765 if something else grabbed it
fuser -k 8765/tcp 2>/dev/null || true
sleep 1

echo "Starting Jarvis..."
PYTHONPATH=/homeassistant nohup "$PYTHON" "$JARVIS_DIR/bot.py" >> "$JARVIS_DIR/jarvis.log" 2>&1 &
echo $! > "$JARVIS_DIR/jarvis.pid"
echo "Jarvis started (pid $!)"
