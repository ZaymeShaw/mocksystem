#!/usr/bin/env bash
# Install eval_harness into mock_system on the Mac.
set -euo pipefail
TARGET="${1:-/Users/xiaozijian/WorkSpace/package/mock_system}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$TARGET/eval_runs"
rsync -a "$SCRIPT_DIR/eval_harness/" "$TARGET/eval_harness/"
echo "Installed -> $TARGET/eval_harness"
echo "eval_runs -> $TARGET/eval_runs"
echo
echo "Smoke:"
echo "  cd \"$TARGET\""
echo "  export PYTHONPATH=eval_harness/src"
echo "  python3 -m eval_harness.run --config eval_harness/config.yaml --cases A01,E01"
echo
echo "Full:"
echo "  python3 -m eval_harness.run --config eval_harness/config.yaml --all"
