#!/bin/sh
set -e
# Run the deployed code in /config/jarvis with the package importable as `jarvis`.
cd /config/jarvis
export PYTHONPATH=/config
echo "[jarvis-addon] starting bot.py with $(python --version)"
exec python /config/jarvis/bot.py
