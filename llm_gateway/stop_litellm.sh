#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${LITELLM_PORT:-4001}"
if [[ -f "$DIR/run/litellm.pid" ]]; then
  pid=$(cat "$DIR/run/litellm.pid")
  kill "$pid" 2>/dev/null || true
  # also kill process group if we started with start_new_session
  kill -- -"$pid" 2>/dev/null || true
  rm -f "$DIR/run/litellm.pid"
  echo "stopped litellm $pid"
fi
if command -v lsof >/dev/null 2>&1; then
  for p in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true); do
    kill "$p" 2>/dev/null || true
    echo "stopped leftover listener $p"
  done
fi
# avoid broad pkill that can race a fresh start
