# mock_system

Local harness for running Claude, Pi, and Insurance QA evaluations through an LLM gateway and inspecting their traces.

## Source layout

- `eval_harness/` — evaluation runners, adapters, trace normalization, HTML/Excel export, configuration examples, schemas, and tests.
- `llm_gateway/` — local LiteLLM gateway, proxy, request attribution, and startup scripts.
- `scripts/` — evaluation orchestration and local Insurance QA integration helpers.
- `mock_run/` and `pi_run/` — isolated agent workspaces and prompt instructions.
- `docs/` — design and operating notes.

## Local inputs and output

Question sets in `eval_harness/data/` and derived bundles in `eval_harness/bundles/` are deliberately not committed. Evaluation configs refer to these local paths; provide your own authorized datasets before running the harness. `eval_runs/`, gateway logs, virtual environments, backups, local MCP settings, and `.env` profiles are also excluded.

Copy `llm_gateway/.env.example` to `llm_gateway/.env` and set your own upstream and local gateway keys. Some scripts and profiles contain machine-specific paths and need adjustment on another workstation.
