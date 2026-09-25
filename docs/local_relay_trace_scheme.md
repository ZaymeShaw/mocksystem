# 完整方案：以本地中转为中心的 Trace 分析

> 状态：已对齐（2026-09-23）；**Phase 0 已落地并冒烟通过（2026-09-23）**；Phase 1 wire-only HTML 入口已加（`llm_trace_html --from-gateway-log`）  
> 依据：方案讨论 + LiteLLM `:4001` / thin `:4000` 上 `eval_case_id` 实测  
> 目标：凡走本地中转、且协议是 OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 的 harness，都能做同一套 trace 分析。  
> 批跑 adapter 短期兼容保留，但不是分析前提。

---

## 1. 目标与非目标

### 要做

- 分析主粮 = 中转落下的统一 `llm_calls`（wire 账本）
- 三种协议进、一种分析视图出
- 每笔调用可归到 `eval_case_id`（无文件回退）
- 无 harness Trace 时，HTML/Excel 仍可用（工具时间线从 messages / tool_calls 还原）
- 现有 Claude 批跑继续能跑；效果不回退

### 不做（本期）

- 不为每个 harness 先写完整 Trace adapter
- 不做评分 / judge
- 不依赖「当前 case 文件」猜归属
- 不强制所有流量改走 thin；LiteLLM `:4001` 仍是主中转（thin 作 raw 兜底或后续边车）

---

## 2. 总架构

```
任意 Harness（Claude / pi / 自研 / SDK）
        │  仅三种协议
        │  body/header 带 eval_case_id
        ▼
┌───────────────────────────────────────┐
│  本地中转（主：LiteLLM :4001）          │
│  1. 读 case_id（body / header / 文本兜底）│
│  2. 记统一 llm_calls                     │
│  3. drop 自定义字段后再打上游             │
└───────────────────────────────────────┘
        │
        ▼
  统一账本 llm_calls.jsonl / per-run 切片
        │
        ├─► Excel（索引 + raw request/response）
        └─► HTML（时间轴 / 上下文 / 并行 tool；可不依赖 harness Trace）

可选：
  eval runner + 薄 adapter ──► 拉起某 harness 跑 Bundle（批跑，非分析必需）
```

两层职责拆开：

| 层 | 职责 | 要不要 adapter |
|----|------|----------------|
| **分析层** | 中转契约 + 归一 llm_calls + Excel/HTML | **否** |
| **执行层** | 起进程、喂 Bundle、收结束 | **要（可选）** |

---

## 3. 中转契约（硬约定）

### 3.1 协议面

| 协议 | 路径（以 LiteLLM 为准） | 实测（2026-09-23） |
|------|-------------------------|-------------------|
| OpenAI Chat Completions | `POST /v1/chat/completions` | body `eval_case_id` → 200，进 `optional_params` |
| OpenAI Responses | `POST /v1/responses` | 同上，200，进 `optional_params` |
| Anthropic Messages | `POST /v1/messages` | body 字段 200 不炸上游；**LiteLLM optional_params 里常丢**；thin 读 raw 可见 |

上游：`drop_params: true`（已有）——自定义字段不应用来依赖「上游保留」，只依赖**中转先读到**。

### 3.2 `eval_case_id` 落点（三种协议分别定义）

字段名统一：`eval_case_id`（字符串，如 `A01`）。

**A. Chat Completions**

```json
{
  "model": "...",
  "messages": [...],
  "eval_case_id": "A01"
}
```

**B. Responses**

```json
{
  "model": "...",
  "input": "...",
  "eval_case_id": "A01",
  "metadata": { "eval_case_id": "A01" }
}
```

顶层必认；`metadata` 作双写增强。

**C. Anthropic Messages**

```json
{
  "model": "...",
  "max_tokens": ...,
  "messages": [...],
  "eval_case_id": "A01"
}
```

中转必须在 **raw body**（或 thin 边车）提取，不能只信 LiteLLM `optional_params`。

**辅路径（三种共用）**

- Header：`X-Eval-Case-Id: A01`

**兜底（兼容现网 Claude 批跑）**

- 文本：`<!--eval_case_id:A01-->`（system / 首条 message；现有逻辑）

**优先级**

1. Header `X-Eval-Case-Id`
2. 结构化 body：`eval_case_id`（及 Responses 的 `metadata.eval_case_id`）
3. 文本标记

冲突：高优先级覆盖低优先级，打 warn。  
都没有：进 **orphan** 桶，**禁止**文件/进程态回退。

**转发前**：从发往上游的 body 删除 `eval_case_id`（header 不转发上游）；`drop_params` 已兜底，仍建议显式剥，行为可预期。

### 3.3 统一 `llm_calls` 记录（分析主粮）

每条至少包含：

| 字段 | 说明 |
|------|------|
| `call_id` | 稳定 id |
| `case_id` | 归一后的 eval_case_id，可空（orphan） |
| `protocol` | `chat_completions` / `responses` / `anthropic_messages` |
| `model` / `stream` / `path` | 路由信息 |
| `ts_start` / `ts_end` / `latency_ms` | 时间 |
| `request` | **原始**请求 JSON（剥密钥后；可保留 eval_case_id 便于核对） |
| `response` | **原始**或拼装后的完整响应 |
| `usage` | 若有（含 cache 相关若上游给） |
| `error` | 若有 |

另产一层可选 **normalized** 视图（给 HTML，不替代 raw）：

- `messages` / `instructions` / `tools`
- `assistant_tool_calls` / `tool_results`
- `cache_read` / `cache_write`（有则填）

Excel：继续「索引列 + request/response JSON」，与现约定一致。  
HTML：声明 **只依赖 llm_calls 即可浏览**；有 harness Trace 则增强展示，无则降级。

---

## 4. 分析前端（与 harness 解耦）

### 输入

- 一次 run 目录：`llm_calls`（按 case 切片或全量 + case_id 过滤）
- 可选：`cases/*/trace.json`（增强）
- 可选：runner 元数据（成功/失败、墙钟）——无则 HTML 只展示 wire

### 能力（wire-only 必须具备）

- 按 case 列出 LLM 调用时间轴
- 单 call 看完整 request/response
- 同轮并行 tool_calls
- 上下文增长（前后 call messages 对比；若客户端只发增量则能力降级并标明）
- 跳转 Excel ↔ HTML（相对路径，可分享 zip）

### 不做

- HTML 里写死 Claude stream-json 事件名作为主路径

---

## 5. 批跑 / Adapter（可选、短期兼容）

```
Bundle + Profile
      │
      ▼
eval runner ──adapter──► Claude Code /（日后）pi …
      │                      │
      │                      └── base_url → 本地中转（打 case_id）
      ▼
 case 目录：墙钟、退出码、可选 Trace
      +
 中转账本归并 → Excel / HTML / share zip
```

Adapter **只负责**：起停、工作目录、喂题、结束判定、按协议注入 `eval_case_id`。  
**不负责**：定义分析 schema（那是中转 + llm_calls）。

短期：Claude adapter 保留；行为不变，逐步改为 body/header 注入，文本标记可并存一版。

---

## 6. 落地分阶段

### Phase 0 — 契约与提取（优先，小改）

1. 文档化三种协议的 `eval_case_id` 落点（上文 §3）——本文即契约底稿
2. 改中转回调 / 必要时 thin：
   - Chat/Responses：读 `optional_params.eval_case_id` + header + 文本兜底
   - Anthropic：读 **raw request body**（LiteLLM `proxy_server_request` 或 thin 统一入口）+ header + 文本
3. 写入 `case_id`；仍无文件回退
4. 冒烟：三种协议各一笔带 `PROBE_*`，确认 jsonl 的 `case_id` 非空且无串案

### Phase 1 — 分析主路径 wire-only

1. ingest / Excel / HTML：明确「无 Trace 可跑」
2. 从 messages 还原 tool 时间线（Anthropic `tool_use` / OpenAI `tool_calls`）
3. share zip / manifest 保持现有固化逻辑

### Phase 2 — 批跑注入对齐

1. Claude adapter：优先 body/header 打 `eval_case_id`，文本标记降级兼容
2. runner 文档：自建流量只要指到中转并带 case_id 即可分析

### Phase 3 — 多 harness（按需）

1. 新框架：先验证「只改 base_url + case_id」能否出 HTML
2. 若要代跑 Bundle，再加薄 adapter
3. 协议归一逻辑继续放在中转，不放在 adapter

---

## 7. 预计代码落点（实现时）

| 模块 | 改什么 |
|------|--------|
| `llm_gateway` 回调（及/或 thin proxy） | case_id 提取优先级；raw body；记 `protocol`；可选剥字段 |
| `eval_harness` ingest | 认新字段；orphan；无 Trace 切片 |
| Excel / HTML / pack-zip | wire-only 路径；文案与依赖说明 |
| Claude adapter | 注入方式升级（Phase 2） |
| Skill 文档 | 中转契约 + 「分析不绑 adapter」 |

**刻意不大拆**「adapters/」大框架，直到 Phase 3 真正有第二个批跑目标。

---

## 8. 风险与对策

| 风险 | 对策 |
|------|------|
| Anthropic 经 LiteLLM 丢自定义字段 | 以 raw/thin 为准（已实测） |
| 客户端不能改 body | header 或文本兜底 |
| 只发增量 messages | HTML 标明「上下文对比可能不完整」 |
| 部分流量没过中转 | 分析覆盖不到；文档要求必经中转 |
| 双写 Trace + llm_calls 不一致 | 展示以 llm_calls 为准，Trace 仅增强 |

---

## 9. 成功标准

1. 仅 curl 三种协议 + `eval_case_id`，不跑 Claude，能出带正确 `case_id` 的账本与 HTML 骨架
2. 现有 Claude 全量链路（文本标记）不回归
3. 故意不带 case_id 的请求进 orphan，不污染其它 case
4. 无 `trace.json` 时 HTML 仍能打开并浏览 llm_calls

---

## 10. 已定案摘要

| 项 | 结论 |
|----|------|
| 分析中心 | 本地中转 |
| 协议 | Completions + Responses + Anthropic |
| 无 harness Trace | 以 wire 为准 |
| 批跑 adapter | 短期兼容，非分析前提 |
| case_id | 要；body 主路径（实测可行）；header 辅；文本兜底；无文件回退 |

---

## 11. 实测摘要（2026-09-23，Mac LiteLLM `:4001`）

| 打法 | HTTP | 上游 | 现有回调可见性 |
|------|------|------|----------------|
| Chat Completions 顶层 `eval_case_id` | 200 | 正常 | 能，在 `optional_params.eval_case_id` |
| Responses 顶层 + metadata | 200 | 正常 | 能，在 `optional_params` |
| Anthropic Messages 顶层 | 200 | 正常 | LiteLLM `optional_params` 常不可见；thin raw `request.eval_case_id` 可见 |
| 仅 Header | 200 | 正常 | 现回调未记 header |
| Chat metadata / litellm_metadata | 200 | 正常 | 未稳定落入当前 optional_params 日志 |

说明：探测时 `case_id` 仍为 null，因提取逻辑当时只认文本 `<!--eval_case_id:…-->`——属实现缺口，不是字段传不过来。

---

## 12. 落地进度

- [x] **Phase 0**：`llm_gateway/callbacks/case_id.py` + `trace_callback.py` / `thin_proxy.py`；优先级 header → body/optional_params → 文本；记 `protocol`；无文件回退。冒烟 `PROBE_*_20260923_124416` Chat/Responses/Anthropic/Header 均非空，control 进 orphan。
- [x] **Phase 1（首批）**：ingest 认 `protocol`；HTML 无 `trace.json` 时 wire-only 总览；`python -m eval_harness.llm_trace_html --from-gateway-log …` 可从账本切片出可浏览 run。
- [x] **Phase 2**：Claude adapter 经 `ANTHROPIC_CUSTOM_HEADERS`（`X-Eval-Case-Id`）+ `CLAUDE_CODE_EXTRA_BODY.eval_case_id` 注入；文本 `<!--eval_case_id:…-->` 仍作兜底。单测 `eval_harness/tests/test_case_id_injection_env.py`；gateway 侧 header 优先已冒烟。
- [x] **Phase 3（接入层）**：`docs/local_relay_harness_connect.md` + `eval_harness.relay_inject`（OpenAI/Agno 与 Claude CLI 注入）。第二个批跑 adapter 等 pi 等目标就绪再开；保险问答类只需 conf `base_url` + 按案注入。
