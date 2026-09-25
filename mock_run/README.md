# mock_run — Claude Code 评测用隔离工作目录

Runner 的 `project_cwd` 指向本目录，避免模型在 `mock_system` 根目录看到 `eval_harness/`、`eval_runs/` 等评测产物。

本目录包含：
- `.mcp.json` — MCP（venv 仍用绝对路径指向 insurance-tools-mcp/.venv）
- `.claude/settings.local.json` — 本地鉴权/环境（勿提交密钥）
- `agent.md` — 系统附加约束

评测产物仍写到 `../eval_runs/`，不写进本目录。
