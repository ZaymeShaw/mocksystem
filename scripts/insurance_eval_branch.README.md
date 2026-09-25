# insurance_eval_branch.py —— 保险问答评测分支维护（仅本地）

## 用途

同事维护的 `insurance-qa-agent`（`origin/main`）会持续更新；评测需要在其之上叠加我们自己的少量改动。
本脚本负责：查看差距、把我们的改动**重放**到最新上游、测试通过后再切换评测分支、导出可交给上游的补丁、回滚。

- 主仓库（同事代码，**不在此脚本里改动**）：`/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent`
- 评测工作树：`/Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent-eval`，检出 `eval/current`
- 路径、分支、标记等默认值写在脚本顶部，也可用 `--repo/--wt/--upstream/--current/--marker/--python/--test-target` 覆盖（**放在子命令之后**）。
- 仅依赖 Python 3 标准库，系统 `python3`（3.9）即可运行。

## 两层提交结构

```
origin/main（上游）
  └─ feat(observability): 可选透传评测 case/execution id …   ← 可合入上游 [upstream-mergeable]
      └─ local(eval): 本地评测配置，指向 :4002 中转 …         ← 仅本地 [local-only]
          = eval/current（评测工作树检出）
```

- **可合入上游**的提交不带标记，可用 `export-patch` 导出交给同事评审。
- **仅本地**的提交 subject 以 `local(eval):` 开头（`LOCAL_MARKER`），含本机中转地址与密钥，**永远不导出、不推送**。
- 新增本地改动时照此约定：通用改动一个提交、不带标记；本机配置另起提交并加 `local(eval):` 前缀。
- `eval/case-id-passthrough` 保留为历史分支（仅透传提交）。所有 `eval/*` 分支都不设上游跟踪。

## 命令

```bash
S=/Users/xiaozijian/WorkSpace/package/mock_system/scripts/insurance_eval_branch.py
python3 $S status [--fetch]         # 上游/当前/基点、落后提交、我方提交分类、工作树与进程
python3 $S sync --dry-run           # 只看计划（不 fetch、不改任何东西）
python3 $S sync [--full-tests [--strict-local]]  # fetch → 重放 → 测试 → 切换 eval/current
python3 $S export-patch [--out DIR] # 导出不带标记的提交，含敏感信息扫描
python3 $S rollback [BRANCH]        # 列出 eval/sync-*；给出分支则把 eval/current 指回去
```

### sync 流程

1. `git fetch origin`（只读；`--no-fetch` 跳过）。若 `eval/current` 已包含上游，提示"无需同步"并退出 0。
2. 我方提交 = `origin/main...eval/current` 的右侧、非合并、且不与上游补丁等价（`--cherry-pick`）；已被上游等价合入的提交会列出并跳过。数量超过 20 视为上游被改写，拒绝继续。
3. 从上游新建 `eval/sync-YYYYMMDD`（重名加 `-HHMM`），在 `/tmp/iqa-sync-*/wt` 临时工作树中按原顺序 `cherry-pick`（不加 `-x`，保持提交信息干净）；应用后为空的提交（上游已含）自动跳过并报告。
4. 用 `.feval_venv` 以 `PYTHONPATH=临时工作树` 运行 `tests/observability/test_eval_passthrough.py`；
   `--full-tests` 另跑全量（忽略 `tests/observability/test_langfuse_live.py`，live 用例本就默认排除）。
   全量失败的用例先在纯上游临时工作树上复跑：上游同样失败的算**环境性存量**（当前环境缺 `asyncmy`、`opentelemetry.exporter`、未设 `core.hooksPath` 等）；
   剩余的新失败再在"上游 + 可合入提交"（首个 `local(eval):` 提交的父提交）上复跑：在那里不失败的算**仅由本地配置引起**，默认只警告（`--strict-local` 则视为失败），其余才算真正引入的失败。
   目前本地配置提交已知会让 4 个配置一致性用例失败（dev 单独开 `tracing_enabled`、`base_url` 指向 :4002），属预期。
   注意：临时工作树在 `/tmp`（macOS 实为 `/private/tmp`），`test_tool_config_reload::test_bad_update_keeps_previous_and_recovers` 会因日志路径含 "private" 失败，上游复跑同样失败，会被归为存量。
5. 通过后检查评测工作树：有已跟踪改动、或有非 shell 进程（命令行含该路径或 cwd 在其中）时拒绝，`--force` 可越过。
6. 若旧 `eval/current` 不在其他 `eval/*` 分支顶端，先备份为 `eval/sync-prev-YYYYMMDD-HHMM`；然后在评测工作树执行 `git checkout -B eval/current <sync 分支>`，移除临时工作树。
7. 打印新旧提交号。**从评测工作树启动的服务需重启**；上游代码已变化，**新评测结果与此前未必可比**。

退出码：0 成功/无需同步；1 一般错误或安全检查拒绝；2 冲突；3 测试失败。

## 冲突处理

`sync` 遇到冲突时会自动 `cherry-pick --abort`、删除临时工作树和失败的 `eval/sync-*` 分支，打印冲突文件后以退出码 2 结束，**`eval/current` 不动**，评测照常可用。手工处理：

```bash
cd /Users/xiaozijian/WorkSpace/package/insurance_qa_agent/insurance-qa-agent
git worktree add --no-track -b eval/sync-YYYYMMDD-manual /tmp/iqa-manual origin/main
cd /tmp/iqa-manual
git cherry-pick <我方提交1>      # 按 status 列出的顺序逐个
# 解决冲突 → git add … → git cherry-pick --continue
PYTHONPATH=$PWD <主仓库>/.feval_venv/bin/python -m pytest tests/observability/test_eval_passthrough.py
cd - && git worktree remove /tmp/iqa-manual
python3 $S rollback eval/sync-YYYYMMDD-manual   # 复用安全检查，把 eval/current 指过去
```

透传提交若冲突，通常是同事改了 `app/main.py` 中间件装配或 `app/runtime/model_transport.py` 的 `ManagedAsyncOpenAI.request`；本地配置提交若冲突，通常是上游改了 `app/conf/models/models_dev_args.yaml` 或 `app/conf/harness/harness_dev_args.yaml`。

## 回滚

```bash
python3 $S rollback                       # 列出 eval/sync-*（* 为当前指向）
python3 $S rollback eval/sync-20260925    # 指回该分支（同样做工作树/进程检查，--force 越过）
```

旧的 `eval/sync-*` 分支不会自动删除；确认不再需要时手工 `git branch -D`。

## 与透传开关的关系

透传提交只在环境变量 `IQA_EVAL_HEADER_PASSTHROUGH=1`（或 true/yes）时装配中间件，把请求头
`X-Eval-Case-Id` / `X-Eval-Execution-Id`（1–128 位 `[A-Za-z0-9._:-]`）带到模型请求；默认关闭，关闭时行为与上游一致。
该变量计划由启动脚本 `start_insurance_qa_local.py` 设置，**目前尚未接入**（需单独批准）；本脚本不设置、不依赖它。

## 禁止推送

- 本脚本**只做本地操作**：唯一的远程交互是只读的 `git fetch`。
- 所有 git 调用经过护栏：参数中出现 `push` 立即中止。
- `export-patch` 只导出不带 `local(eval):` 标记的提交，并扫描 `sk-` 形式密钥、`4002` 端口、YAML `api_key:` 行、长字面量 `api_key`，以及本地提交中出现过的 api_key 实际值；命中即删除导出目录并中止。
- 不要对任何 `eval/*` 分支设置上游或执行 `git push`；补丁交给同事时走人工评审渠道。
