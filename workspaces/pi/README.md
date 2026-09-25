# Pi 工作目录

Pi adapter 使用此目录作为 `project_cwd`。`agent.md` 为附加系统提示词，本地 `.mcp.json` 提供 Pi 使用的 MCP 配置，不提交到仓库。

配置见 `../../eval_harness/configs/profiles/pi.yaml`。provider 和模型设置由 profile 指定，对应 provider 需在本机 Pi 配置中可用。报告写入仓库的 `eval_runs/`。
