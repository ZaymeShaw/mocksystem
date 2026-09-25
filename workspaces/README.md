# Agent 工作目录

这里存放 CLI agent 启动时的 `project_cwd`，Python 实现位于 `eval_harness/src/eval_harness/adapters/`。

- `claude/`：Claude Code 的提示词及本地 MCP、鉴权配置。
- `pi/`：Pi 的提示词及本地 MCP 配置。
- Insurance 调用外部 HTTP 服务，没有仓库内的 CLI 工作目录。

Git 只跟踪每个子目录的 `agent.md` 和 `README.md`。会话、插件、临时文件、MCP 和鉴权配置保留在本机。新接入的 CLI harness 可以增加 `workspaces/<name>/`，并在 profile 中设置 `project_cwd`。

原 `mock_run/` 迁移到 `workspaces/claude/`，原 `pi_run/` 迁移到 `workspaces/pi/`。已有本地文件随目录迁移；历史轨迹中保存的原路径作为运行记录保留。
