#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/run/thin.pid" ]]; then
  pid=$(cat "$DIR/run/thin.pid")
  kill "$pid" 2>/dev/null || true
  rm -f "$DIR/run/thin.pid"
  echo "stopped thin proxy $pid"
else
  echo "no thin.pid"
fi
pkill -f "thin_proxy.py" 2>/dev/null || true
