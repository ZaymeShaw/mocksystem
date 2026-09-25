#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
if [[ -f "$DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$DIR/.env"
  set +a
fi
export UPSTREAM_API_BASE="${UPSTREAM_API_BASE:-https://dashscope.aliyuncs.com/apps/anthropic}"
export UPSTREAM_API_KEY="${UPSTREAM_API_KEY:-${ANTHROPIC_API_KEY:-}}"
export LLM_GATEWAY_UPSTREAM="${LLM_GATEWAY_UPSTREAM:-$UPSTREAM_API_BASE}"
export LLM_GATEWAY_LOG_DIR="${LLM_GATEWAY_LOG_DIR:-$DIR/logs}"
export LLM_GATEWAY_PORT="${LLM_GATEWAY_PORT:-4000}"
export LLM_GATEWAY_HOST="${LLM_GATEWAY_HOST:-127.0.0.1}"
mkdir -p "$DIR/logs" "$DIR/run"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
if [[ -f "$DIR/run/thin.pid" ]] && kill -0 "$(cat "$DIR/run/thin.pid")" 2>/dev/null; then
  echo "thin proxy already running pid=$(cat "$DIR/run/thin.pid")"
  exit 0
fi
# shellcheck disable=SC1091
source "$DIR/.venv/bin/activate"
nohup python "$DIR/thin_proxy.py" --port "$LLM_GATEWAY_PORT" >"$DIR/logs/thin_proxy.stdout.log" 2>&1 &
echo $! >"$DIR/run/thin.pid"
sleep 0.8
echo "started thin proxy pid=$(cat "$DIR/run/thin.pid") http://$LLM_GATEWAY_HOST:$LLM_GATEWAY_PORT upstream=$LLM_GATEWAY_UPSTREAM"
