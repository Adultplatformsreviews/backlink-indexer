#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8787}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
