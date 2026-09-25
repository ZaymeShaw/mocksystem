#!/usr/bin/env bash
# Production dual-eval wrapper → eval_harness.dual_run
# Usage: scripts/run_dual_dataset_a.sh [A01,A02] [-- extra dual_run args]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$ROOT/eval_harness"
CASES="${1:-A01,A02}"
if [[ "${1:-}" == -* ]]; then
  CASES="A01,A02"
else
  shift || true
fi
DUAL_ARGS=(
  --bundle "$HARNESS/bundles/120_prompt_only_v1.jsonl"
  --cases "$CASES"
)
if [[ "$#" -gt 0 ]]; then
  DUAL_ARGS+=("$@")
fi
cd "$HARNESS"
exec env PYTHONPATH=src python3 -m eval_harness.dual_run "${DUAL_ARGS[@]}"
