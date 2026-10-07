#!/usr/bin/env bash
# Linux / macOS launcher. Uses ./.venv if it exists, otherwise the python3
# on PATH. Install dependencies first (see README "Linux / VPS deployment").
set -e
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then
    . .venv/bin/activate
fi
exec python3 claude_bridge.py "$@"
