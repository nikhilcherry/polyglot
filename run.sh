#!/usr/bin/env bash
# One command: set up the venv (first run) and start polyglotd with the simulated hub.
#   ./run.sh                     simulator + virtual living room
#   ./run.sh --serial /dev/ttyUSB0   real ESP32 running firmware/polyglot_hub
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -e ".[dev]"
fi
echo "Open http://127.0.0.1:8770"
exec .venv/bin/python -m polyglot.daemon "$@"
