#!/usr/bin/env bash
# Triple eval on shared 0922 → eval_harness.dual_run --agents claude,insurance,pi
# Usage: scripts/run_triple_0922.sh [A01,C20] [-- extra dual_run args]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$ROOT/eval_harness"
CASES="${1:-A01,C20}"
if [[ "${1:-}" == -* ]]; then
  CASES="A01,C20"
else
  shift || true
fi
ARGS=(
  --bundle "$HARNESS/bundles/120_prompt_only_0922_shared.jsonl"
  --claude-config "$HARNESS/config_claude_0922.yaml"
  --pi-config "$HARNESS/config_pi_0922_shared.yaml"
  --agents claude,insurance,pi
  --cases "$CASES"
)
if [[ "$#" -gt 0 ]]; then
  ARGS+=("$@")
fi
cd "$HARNESS"
exec env PYTHONPATH=src python3 -m eval_harness.dual_run "${ARGS[@]}"
