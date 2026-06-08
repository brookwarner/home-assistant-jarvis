#!/bin/sh
# Jarvis supervisor — the watchdog that actually works.
#
# WHY THIS EXISTS: the HA Core watchdog (shell_command.restart_jarvis, called every
# 5 min) runs in the HA Core container, which cannot see /homeassistant/jarvis or its
# venv — so it has never been able to (re)start Jarvis. This script runs INSIDE the
# SSH/terminal add-on container, where Jarvis actually lives, and keeps it alive.
#
# start.sh is idempotent: it no-ops if Jarvis is already running, starts it if not,
# and force-restarts if a .force_restart sentinel is present. So we just call it on a
# loop. On the very first pass it will consume any pending .force_restart and bring
# Jarvis up on the latest code.
#
# BOOTSTRAP (run once in the Terminal/SSH add-on):
#   nohup sh /homeassistant/jarvis/supervise.sh >/dev/null 2>&1 &
#
# PERSIST ACROSS ADD-ON RESTARTS: add that same line to the add-on's `init_commands`.

JARVIS_DIR=/homeassistant/jarvis
LOCK="$JARVIS_DIR/.supervise.lock"
INTERVAL=60

# Single-instance guard: if another supervisor is alive, exit.
if [ -f "$LOCK" ]; then
    LPID=$(cat "$LOCK" 2>/dev/null)
    if [ -n "$LPID" ] && kill -0 "$LPID" 2>/dev/null; then
        echo "supervise.sh already running (pid $LPID); exiting"
        exit 0
    fi
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM

echo "Jarvis supervisor started (pid $$), checking every ${INTERVAL}s"
while true; do
    sh "$JARVIS_DIR/start.sh" >> "$JARVIS_DIR/jarvis.log" 2>&1 || true
    sleep "$INTERVAL"
done
