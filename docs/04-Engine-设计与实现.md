# 04 · Engine 设计与实现

> **当前实现说明**：Engine 是 Smith 的可复用 Python 运行时，不负责 HTTP 或终端渲染。稳定入口是 `engine.execution.lifecycle`；具体功能以对应模块和 `engine/tests/` 为准。

## 模块地图

| 模块 | 责任 | 代表文件 |
| --- | --- | --- |
| `execution/` | Run 生命周期、事件、编排、pipeline、ReAct 与恢复 | `orchestration/lifecycle.py`、`react/react_loop.py` |
| `context/` | Prompt 装配、预算、历史压缩与上下文 fitting | `assembler.py`、`budget.py`、`compression.py` |
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

`run_agent_stream()` 有三条互斥路径：

1. 用户显式指定 skill：加载并执行该 skill；
2. route 无 pipeline 或 pipeline 资源不可用：直接 ReAct；
3. route 指向可用 pipeline：顺序执行 `SkillChain`，每个节点的 base gate 与领域 gate 都通过后才提交输出。

`agents/pipelines/coding.yaml` 是当前唯一 shipped pipeline。它使用 `understanding → planning → [architecture] → implementation → validation`，并声明从失败阶段回退的边。

### ReAct 与事件

`react_event_loop()` 是统一 Agent 循环：发送已拟合的 messages 与工具 schema，归并 `ProviderEvent`，流式产生文本/工具/用量/终态事件，再把工具结果写回下一轮上下文。它显式限制最大轮数、工具调用预算、连续工具错误、长度截断后的续写和 preflight 挑战。

`ExecutionEvent` 是对外可观察语义。重要类别包括：route 决定、skill 开始/结束、文本 delta、工具调用及结果、gate 结果、审批请求、provisional commit/retract、context usage 与 done。Shell/Server 只消费事件，不需要了解某个 Provider 的原始帧格式。

### provisional 输出

直接 ReAct 的文本可持续流式呈现。Pipeline 节点的文本先以 `provision_id` 标记为候选输出：gate 通过后发送 `PROVISIONAL_COMMIT`，失败或回退时发送 `PROVISIONAL_RETRACT`。这避免被拒绝的草稿进入最终 transcript、session 或记忆。

## 安全与副作用

工具执行不是“模型返回了函数名就运行”。执行顺序是：

```text
Tool schema / registry
  → ToolPolicy（静态与动态策略）
  → ToolGuard（路径、命令、凭据、会话白名单）
  → FactGate（需要先取证时的挑战）
  → ApprovalBroker（需要用户授权时）
  → provider.execute()
  → ToolExecutionLedger / ExecutionEvent
```

`ToolGuard` 是最后的强制边界。新 provider 必须通过 registry 注册，并让涉及路径、shell、网络或外部副作用的参数进入相应安全检查；不得由 skill 或 MCP 旁路直接执行。

## Run、恢复与可观测性

`RunStateStore` 将状态落入 agent runtime 数据目录。lifecycle 在服务启动时把遗留的活跃 Run 置为可恢复，pipeline checkpoint 只会被同身份、同工作目录、同请求且 owner 已结束的 Run 采用。重复提交不能接管仍在运行的 Run checkpoint。

每个 Run 的关键事件可投影成摘要、trace、诊断、健康和 incident 信息。Server 暴露这些只读视图；具体 API 见[Server 文档](07-Server-平台后端.md)。

## 深入阅读

- [04a · ReAct Loop](04a-ReAct-Loop-设计.md)：事件、预算、流式与压缩的细节；
- [04b · LLM 模块](04b-LLM模块设计.md)：provider 和配置；
- [05 · 记忆系统](05-Engine-记忆系统.md)：学习闭环与数据格式；
- [06 · Agents 内容层](06-Agents-内容层.md)：identity、pipeline、skill 与 tool provider 的编辑方式；
- [ADR 0001](adr/0001-approval-gated-host-capabilities.md)：host capability 审批决策。

## 非目标

Engine 当前不提供 multi-agent scheduler、plugin handler/runtime、外部 webhook trigger、RAG 服务或 MCP server。若未来引入，必须先定义它们对 Run 所有权、资源关闭、并发写工作目录、审批传播和观测链路的影响。
