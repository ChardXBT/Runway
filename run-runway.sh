#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

if [ ! -x .venv/bin/python ]; then
  echo "Missing .venv. Run: python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.lock && .venv/bin/python -m pip install --no-deps -e ." >&2
  exit 1
fi

# The API refuses a database behind the migration head; migrate explicitly first.
.venv/bin/python -m runway.cli.main db upgrade
.venv/bin/python -m uvicorn runway.api.app:app --host 127.0.0.1 --port 8000 &
API_PID=$!
.venv/bin/python -m runway.cli.main worker &
WORKER_PID=$!
trap 'kill "$API_PID" "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
npm run web:dev
