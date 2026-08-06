# 04 · Engine 设计与实现

> **当前实现说明**：Engine 是 Smith 的可复用 Python 运行时，不负责 HTTP 或终端渲染。稳定入口是 `engine.execution` 包本身——它重导出 `run_stream_with_runtime` / `resume_stream_with_runtime` / `reply_with_runtime` / `run_memory_daily_tick` / `run_memory_idle_tick` 等运行契约；`orchestration/`、`pipeline/`、`react/` 是实现细节。具体功能以对应模块和 `engine/tests/` 为准。

## 模块地图

| 模块 | 责任 | 代表文件 |
| --- | --- | --- |
| `execution/` | Run 生命周期、事件、编排、pipeline、ReAct 与恢复 | `orchestration/lifecycle.py`、`react/react_loop.py` |
| `execution/routing/` | 请求到 identity route 的确定性路由 + 可选 LLM 兜底 | `task_router.py`（`route_task` + `route_task_with_llm`） |
| `execution/hooks/` | 工具生命周期 Hook 与 engine 扩展 Hook 两套框架 | `tool/{interface,loader,manager}.py`、`extension/manager.py` |
| `context/` | Prompt 装配、预算、历史压缩与上下文 fitting | `assembler.py`、`budget.py`、`compression.py`、`fitting.py` |
| `identity/` | YAML identity 解析、校验和关键词路由 | `catalog.py` |
| `llm/` | Provider 无关契约、adapter、配置和使用量 | `port.py`、`client.py`、`factory.py` |
| `tool/` | 工具 schema、注册、调用账本和截断 | `registry.py`、`schema.py`、`ledger.py` |
| `skill/` | SKILL.md 发现、启用状态与执行 | `loader.py`、`registry.py`、`executor.py` |
| `safety/` | 工具策略、文件/命令 guard、审批与事实检查 | `tool_guard.py`、`approval.py` |
| `memory/` | 证据、编译、审核、Nudge、Dream 与检索 | `store.py`、`compile.py`、`maintenance.py` |
| `mcp/` | stdio/streamable HTTP client 与 session pool | `client.py`、`config.py`、`session_pool.py` |
| `observability/` | Run trace、摘要、健康、事故与诊断投影 | `recorder.py`、`projections.py` |

## 核心契约

### 运行时装配

调用者提交 `EngineRequest`、`RuntimeContext` 与 `RuntimeServices`。

- `EngineRequest` 是一条用户输入及其 history、context、强制 skill、identity 与工作目录；
- `RuntimeContext` 是已可信解析的 agent/profile/session/filesystem 边界，Engine 不从自身 cwd 猜测项目目录；
- `RuntimeServices` 拥有当前 Run 的 LLM、tools、skills、安全对象、MCP clients、hooks 与 observation factory，并负责关闭归属资源。

这让 Engine 可由 Server、测试或其他可信嵌入层调用，而不引入 FastAPI、SQLite 或 Shell 依赖。

### 路由与流程

`IdentityCatalog` 从 `agents/identities/*.yaml` 加载唯一默认 identity 和可选 routes。每条 route 包含关键词、示例、优先级与可选 pipeline；英文关键词使用词边界和受限屈折匹配，避免 `git` 命中 `digital` 一类误路由。

确定性匹配之外还有一层可选的 LLM 兜底：`route_task_with_llm()` 在关键词/示例 miss 且 `allow_llm_fallback=True` 时，让模型从**已声明的** route token（`identity:route`）或 `DIRECT` 中选择一个，不能臆造 identity 或 pipeline；该兜底默认关闭——自动路由处在交互关键路径上，不应为每条普通消息隐藏一次模型往返。

`run_agent_stream()` 有三条互斥路径：

1. 用户显式指定 skill：加载并执行该 skill；
2. route 无 pipeline：直接 ReAct；
3. route 指向 pipeline：顺序执行 `SkillChain`。每个节点先执行声明的 Skill；若 Skill 缺失、被禁用、
   运行失败或未通过验收，则在该节点以 ReAct 补偿，并沿用前序产物、节点工具范围和同一套 gate；
   只有 gate 通过后才提交输出并进入下一节点。

路径 1 有一个例外（`grill_me_chain_entry`）：`forced_skill == "grill-me"` 且 route 判定为
`requirements-research` 时，不走一次性 forced-skill 执行，而是进入完整的
requirements-research pipeline——`grill-me` 是该链的用户侧入口包装。

当前有三条 shipped pipeline（`agents/pipelines/requirements-research.yaml`、
`tdd-development.yaml`、`code-review.yaml`），分别对应 `coding` 身份的三条意图路由：

| pipeline | 步骤（skill → gate） |
|---|---|
| `requirements-research` | `grilling`→`grilling_complete`、`research`→`research_brief`、`ecc-plan`→`plan_confirmed` |
| `tdd-development` | `diagnosing-bugs`→`red_loop`（条件 `coding_bugfix_needs_diagnosis`）、`tdd-workflow`→`tdd_evidence`、`verification-loop`→`tdd_verification` |
| `code-review` | `code-review`→`review_report`、`verification-loop`→`review_verification` |

普通编码、修复与重构请求不命中关键词路由，留在直接 ReAct。每条链都禁止把副作用或
真实测试结果只写在 Markdown 里：它们必须由 skill 的工具调用和 gate 的事实检查产生。

### ReAct 与事件

`react_event_loop()` 是统一 Agent 循环：发送已拟合的 messages 与工具 schema，归并 `ProviderEvent`，流式产生文本/工具/用量/终态事件，再把工具结果写回下一轮上下文。它显式限制最大轮数、工具调用预算、连续工具错误、长度截断后的续写和 preflight 挑战。

`ExecutionEvent` 是对外可观察语义。重要类别包括：route 决定、skill 开始/结束、文本 delta、工具调用及结果、gate 结果、审批请求、provisional commit/retract、context usage 与 done。Shell/Server 只消费事件，不需要了解某个 Provider 的原始帧格式。

### provisional 输出

直接 ReAct 的文本可持续流式呈现。Pipeline 节点的文本先以 `provision_id` 标记为候选输出：gate 通过后发送 `PROVISIONAL_COMMIT`，失败或回退时发送 `PROVISIONAL_RETRACT`。这避免被拒绝的草稿进入最终 transcript、session 或记忆。

## 安全与副作用

工具执行不是“模型返回了函数名就运行”。执行顺序是：

```text
Tool schema / registry
  → 可见性检查（节点级 registry 隐藏的工具直接拒绝，不进入策略）
  → ToolPolicy{ToolGuard（路径、命令、凭据、会话白名单）, FactGate（先取证挑战）}
  → ApprovalBroker（需要用户授权时挂起等待）
  → PreToolHook（如 config-protection，可拒绝）
  → registry.authorize_execution + execute（内含 ToolExecutionLedger 幂等）
  → PostToolHook（warnings 注入对话）
  → ExecutionEvent
```

`ToolGuard` 是最后的强制边界。新 provider 必须通过 registry 注册，并让涉及路径、shell、网络或外部副作用的参数进入相应安全检查；不得由 skill 或 MCP 旁路直接执行。

## Run、恢复与可观测性

`RunStateStore` 将状态落入 agent runtime 数据目录。lifecycle 在服务启动时把遗留的活跃 Run 置为可恢复。pipeline checkpoint 的采用分两种情况：崩溃恢复要求严格同请求——同 agent、同 identity、同工作目录、同 route、节点 index 有效，且消息与原请求完全一致，恢复到下一个未完成节点；`awaiting_user_input` 暂停恢复只要求同 scope，新消息即被视为节点提问的答案（写入 `CTX_USER_RESPONSE`），恢复到同一节点继续。两种情况都要求 checkpoint 的 owner Run 已结束——重复提交不能接管仍在运行的 Run checkpoint。

每个 Run 的关键事件可投影成摘要、trace、诊断、健康和 incident 信息。Server 暴露这些只读视图；具体 API 见[Server 文档](07-Server-平台后端.md)。

## 深入阅读

- [04a · ReAct Loop](04a-ReAct-Loop-设计.md)：事件、预算、流式与压缩的细节；
- [04b · LLM 模块](04b-LLM模块设计.md)：provider 和配置；
- [05 · 记忆系统](05-Engine-记忆系统.md)：学习闭环与数据格式；
- [06 · Agents 内容层](06-Agents-内容层.md)：identity、pipeline、skill 与 tool provider 的编辑方式；
- [ADR 0001](adr/0001-approval-gated-host-capabilities.md)：host capability 审批决策。

## Hook 框架

`engine.execution.hooks` 是统一导入路径，下面是两套独立的 Hook 系统：

- **工具生命周期 Hook**（`hooks/tool/`）：`PreToolHook`（可拒绝工具执行）/ `PostToolHook`（产出 warnings）/ `StopHook`（响应结束批处理），由 `HookRegistry` 注册执行，`HookLoader` 支持从 YAML 配置动态加载外部 Python 类（`agents/smith/hooks.yaml`）。目前只接入直接 ReAct 路径。
- **engine 扩展 Hook**（`hooks/extension/`）：`HookManager` / `HookType`，用于 prompt 改写、记忆生命周期 tick、after-turn 持久化等引擎内扩展点，派发策略有 `FIRST` / `SERIES` / `SERIES_MERGE` / `SERIES_LAST` / `PARALLEL` 五种。

## 非目标

Engine 当前不提供 multi-agent scheduler、外部 webhook trigger、RAG 服务或 MCP server。若未来引入，必须先定义它们对 Run 所有权、资源关闭、并发写工作目录、审批传播和观测链路的影响。
