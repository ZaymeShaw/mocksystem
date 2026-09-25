# Claude Code Eval Harness

批量把数据集问句跑过 Claude Code CLI，捕获完整轨迹（thinking / tool_use / tool_result / 多轮 / 耗时 / first-frame / 最终文本），并双轨输出 `results.xlsx` + 每题 `trace.json` / `stream.jsonl`。

**不做评分。** 勿把 `.claude/settings.local.json` 里的密钥写入 traces / Excel。

## 目录

```
eval_harness/
  README.md
  SKILL.md
  requirements.txt
  config.yaml / config.example.yaml
  data/                 # 问句版 + gold 元数据（gold 不进 prompt）
  src/eval_harness/
    parse_dataset.py
    claude_adapter.py
    normalize_trace.py
    excel_export.py
    run.py
eval_runs/<run_id>/     # 运行产物（在 mock_system 根下）
```

## 依赖

- Claude Code CLI（本机已装：`/Users/xiaozijian/.npm-global/bin/claude`）
- Python 3.10+
- `openpyxl`、`PyYAML`（本机 python3 通常已有 openpyxl）

```bash
python3 -m pip install -r eval_harness/requirements.txt
```

## 运行前提

- **必须**在 `mock_system` 工程 cwd 下跑 Claude（config 里 `project_cwd`），以便加载 `.claude/settings.local.json` 的网关/鉴权。
- 使用 `agent.md`（不是 CLAUDE.md），通过 `--append-system-prompt "$(cat agent.md)"` 注入。
- stdin 重定向 `/dev/null`（harness 已处理）。

## CLI

在 `mock_system` 根目录：

```bash
export PYTHONPATH=eval_harness/src

# 冒烟：A01 + E01
python3 -m eval_harness.run --config eval_harness/config.yaml --cases A01,E01

# 区间
python3 -m eval_harness.run --config eval_harness/config.yaml --cases A01-A05

# 全量 120 题（串行 concurrency=1）
python3 -m eval_harness.run --config eval_harness/config.yaml --all

# 只解析数据集
python3 -m eval_harness.run --config eval_harness/config.yaml --all --dry-parse
```

## 每题产物

`eval_runs/<run_id>/cases/<case_id>/`

- `prompt_turns.json`
- `stream_turnN.jsonl` + 合并后的 `stream.jsonl`
- `trace.json`（规范化轨迹）
- `events.jsonl`
- `session_transcript.jsonl`（若能从 `~/.claude/projects/...` 拷到）
- `argv_turnN.json`（不含密钥）

运行级：`results.xlsx`、`run_meta.json`

## Excel sheets

- **cases**：每题一行（指标 + final_preview≤500 + trace 相对路径）
- **tool_calls**：工具调用预览
- **run**：run_id / started_at / claude_version / dataset / cwd / notes

## 数据集

- 问句版（用于跑）：`data/120题-仅问句版(1).md` — `**A01**` + 问句；E/F 用 `第N轮：`
- Gold（元数据，不进 prompt、不评分）：`data/复杂协同与上下文问题集-120题(2).md`

## 常见问题

- **402 / auth**：确认 cwd=`mock_system`，且 `.claude/settings.local.json` 有效；配额恢复后原命令重跑即可。
- **thinking 缺失**：stream 没有时会尝试拷 session transcript。
- **多轮**：第 1 轮从 `type=result` 取 `session_id`，后续 `--resume`。
