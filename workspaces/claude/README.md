# Claude Code 工作目录

Claude adapter 使用此目录作为 `project_cwd`。`agent.md` 为附加系统提示词；本地 `.mcp.json` 配置 MCP，`.claude/settings.local.json` 配置鉴权与环境。后两者不提交。

配置见 `../../eval_harness/configs/profiles/claude.yaml`。报告由评测框架写入仓库的 `eval_runs/`。网关启动时通过 `llm_gateway/sync_claude_settings.py` 同步此处的本地鉴权设置。
