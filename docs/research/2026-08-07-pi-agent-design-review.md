# Pi Agent 设计复核：对 Agent-Smith 内核收敛的启示

> 调研日期：2026-08-07  
> 基线：Pi `v0.84.0`，发布于 2026-08-06，对应 commit [`a5f43bf8aff3c55752432655f7334e3dafd1e256`](https://github.com/earendil-works/pi/commit/a5f43bf8aff3c55752432655f7334e3dafd1e256)。  
> 范围：只使用 Pi 官方仓库源码和官方文档；源码链接固定到上述 commit。本文是设计比较，不代表 Agent-Smith 已支持 Pi 的能力，也不建议直接移植实现。

## 结论

Pi 真正值得借鉴的不是“少写安全代码”，而是**让核心只拥有一次决策**：一个通用 Agent loop 负责模型、工具调用和消息循环；会话、压缩、资源加载和 UI 放在 coding-agent 层；模型差异放在 provider 层；工作流与产品偏好交给 skills/extensions。

```text
pi-coding-agent：session、compaction、resources、extensions、CLI/TUI
        │
        ▼
pi-agent-core：单一 Agent loop、消息状态、工具执行、事件
        │
        ▼
pi-ai：provider、model catalog、auth、协议适配、stream
```

官方将仓库边界概括为 `ai / agent / tui / coding-agent` 四个包；`pi-agent-core` 本身只依赖统一流接口，不认识具体 CLI 或供应商协议。[项目结构](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/development.md)；[agent-core README](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/agent/README.md)

对 Smith 的直接判断是：**应复制 Pi 的边界，不应复制 Pi 的威胁模型。** Smith 的本地常驻助手定位仍需要硬性 `ToolGuard`、审批和记忆；但 routing、pipeline、ReAct、policy、guard、approval、ledger 不应分别拥有一套工具决策。

## 1. Agent loop：一个循环，产品行为从回调接入

Pi 的低层循环基本是：

1. 将 `AgentMessage[]` 经 `transformContext` 和 `convertToLlm` 转为供应商可理解的消息；
2. 调用注入的 `StreamFn`，流式生成 assistant message；
3. 提取 tool calls；参数先按 TypeBox schema 校验；
4. `beforeToolCall` 可阻断；通过的调用默认并行执行，声明为 sequential 的工具会让该批次串行；
5. `afterToolCall` 可改写结果，tool results 按原调用顺序写回上下文；
6. 有工具调用就进入下一轮；没有时再处理 steering/follow-up 队列并结束。

这个循环同时发出 `agent/turn/message/tool_execution` 事件，UI、持久化和扩展订阅同一事件流，不各自实现另一套循环。[agent-loop.ts](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/agent/src/agent-loop.ts)；[工具与事件类型](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/agent/src/types.ts)

关键点不是 loop 功能少，而是它只有两个稳定插口：执行前决策和执行后变换。重试、自动 compaction、session 写入、extension 事件等由上层 `AgentSession` 编排。[coding-agent SDK 装配](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/sdk.ts)；[AgentSession](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/agent-session.ts)

Smith 当前则先在 `run_agent_stream` 分成 forced skill、direct ReAct 和 pipeline；direct 路径中的 `react_event_loop` 再构造 `ToolPolicy`，后者组合 `ToolGuard` 与 `FactGate`；`ToolRegistry.execute` 又做 normalization、guard backstop、approval binding、execution ledger、environment dispatch 和结果截断。每一层都有合理目的，但同一次调用被多层共同解释，形成修改扩散。

## 2. 工具模型：默认四个，能力七个，一份 schema

Pi 默认只给模型四个工具：`read`、`write`、`edit`、`bash`。另外提供 `grep`、`find`、`ls` 三个可选内置工具；CLI/SDK 可用 allowlist 或 exclude list 选择启用项。[coding-agent README](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/README.md)；[工具工厂与工具集](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/tools/index.ts)

一个低层 `AgentTool` 的核心契约只有：

```text
name + description + TypeBox parameters + execute
                + label / executionMode（可选）
```

schema 在 loop 中统一校验后才进入 preflight；工具失败通过抛错统一变成 `isError` tool result。coding-agent 再用 `ToolDefinition` 增加 prompt snippet、显示和自定义渲染等产品信息。[AgentTool 类型](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/agent/src/types.ts)；[extension 工具定义](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/extensions/types.ts)

Pi 的系统提示词也从**当前实际启用的工具**生成：当 `grep/find/ls` 未启用而有 `bash` 时，只提示模型用 bash 做文件发现，不需要让每个专用工具常驻上下文。[system-prompt.ts](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/system-prompt.ts)

这给 Smith 的启示不是删掉所有专用工具。专用 read-only 工具仍可提供更稳定的输出和更窄的权限；真正应收敛的是：

- 默认编码 profile 只暴露最常用的小集合；Web、PDF、Git、记忆管理等按任务启用；
- 每个工具只有一份 canonical contract，由它派生模型 schema、风险、审批摘要、并发性、执行环境、幂等性和输出上限；
- normalize/validate 只做一次，之后所有 guard 和执行器消费同一个 typed call。

## 3. Session 与 context：追加式树，而不是隐藏状态机

Pi session 是 append-only JSONL：每条 entry 通过 `id/parentId` 构成树，当前 leaf 决定送给模型的活跃分支。消息、模型切换、thinking level、compaction、branch summary、label 和扩展状态都写成显式 entry；完整历史保留在文件里。[Session 格式](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/session-format.md)；[SessionManager](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/session-manager.ts)

上下文接近上限时，Pi 总结旧消息并保留近期消息；摘要本身也成为 entry，因此 compaction 是可见、可恢复的投影，而不是覆盖原历史。分支切换也可保存离开分支的摘要。[compaction 设计](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/compaction.md)

Pi 没有内建跨 session 的语义记忆库。项目 context 来自 `AGENTS.md/CLAUDE.md`；skills 启动时只把 name/description 放入提示词，需要时模型再用普通 `read` 加载完整 `SKILL.md`，即 progressive disclosure，不需要专门的 `skill_load` 工具。[skills 文档](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/skills.md)；[skills 实现](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/skills.ts)

Smith 不宜因此删除 memory：跨会话积累正是产品差异。可借鉴的是把“会话事实”“长期记忆”“运行 checkpoint”明确分开，并让副作用、审批与恢复状态通过统一事件/记录可审计。是否从 SQLite 改成 JSONL 并不重要，重要的是只有一个权威状态来源。

## 4. Extensions：统一扩展缝，但不是免费的简单

Pi extension 是运行在进程内的 TypeScript 模块。它能注册工具、命令、快捷键、provider 和 UI，也能在 context、provider 请求、tool call/result、session、compaction 等事件上拦截或修改行为。[extensions 文档](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/extensions.md)；[extension runner](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/src/core/extensions/runner.ts)

这解释了 Pi 为什么不内建 plan mode、todo、sub-agent 或 permission popup：它们可以由同一个扩展面实现。代价也很明确：extension API 本身很宽，第三方模块拥有进程权限；文档还明确说明 `tool_call` handler 修改已校验参数后**不会重新校验**。所以 Pi 是“薄策略核心 + 宽扩展面”，不是整个系统天然简单。

对 Smith，目前 skills 已覆盖主要工作流需求，不必为了模仿 Pi 建一个同等宽度的 extension 平台。若要扩展，优先只开放三个窄接口：注册工具、执行前策略、执行后结果；其余能力在出现稳定需求后再增加。

## 5. Provider：协议差异止于模型边界

`pi-ai` 把 provider 定义为运行时单元：provider 自己拥有 model catalog、认证解析和 stream 行为；多个 provider 可共享 Anthropic、OpenAI Responses、OpenAI Completions、Google 等 wire-protocol 实现。`Models` 集合只负责按 model 找到 owner 并路由请求。[pi-ai README](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/ai/README.md)；[Models 实现](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/ai/src/models.ts)

模型能力不是被抹平成最低公分母：reasoning、input 类型、context window、strict tools、transport 和兼容开关都保留为 model metadata；但 agent loop 仍只面对 `Model + Context + StreamFn`。Smith 已有 `LLMPort` 方向，应该继续坚持：供应商认证、header、协议和能力协商留在 adapter，不进入 routing、pipeline 或工具安全层。

## 6. 权限与 sandbox：Pi 选择了另一种威胁模型

Pi 官方明确说明：

- 没有内建 filesystem/process/network/credential 权限系统；
- built-in tools 和 extensions 以启动用户的完整权限运行；
- project trust 只控制是否加载项目的 settings/extensions/skills 等资源，不限制启动后的工具行为；
- 不可信或无人值守任务应把整个进程放进容器、VM、micro-VM 或策略 sandbox，或者由 extension 把工具执行路由进隔离环境。

来源：[安全模型](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/security.md)；[容器化方案](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/docs/containerization.md)

这确实让 Pi 核心更简单，但它是把安全复杂度移到 OS/容器/用户，而不是消灭复杂度。Smith 面向长期运行的个人助手，不应照搬“无审批、宿主机全权限”。更合适的收敛是：

```text
ToolContract
  → 一次参数规范化与校验
  → 一次统一 PolicyDecision（allow / deny / approve）
  → 不可绕过的执行环境边界
  → 一次结果截断、审计与事件发布
```

保留 `ToolGuard` 作为不可绕过边界；把 `FactGate`、审批判断和其他软规则变成同一 policy pipeline 的 checker，并输出一个结构化 decision。Registry 内的 backstop 可以保留，但只验证已经绑定的 decision/call fingerprint，不重新推导一遍策略。

## 7. Pi 的“刻意不做”

Pi 官方设计原则明确不内建 MCP、sub-agents、permission popups、plan mode、todos 和 background bash；这些能力交给 extension/package 或容器、tmux 等外部工具。[设计原则](https://github.com/earendil-works/pi/blob/a5f43bf8aff3c55752432655f7334e3dafd1e256/packages/coding-agent/README.md)

因此，“Pi 简单”的准确含义是：

- agent loop 只有一份；
- 默认工具面很小；
- workflow 不是内核原语；
- provider、session、UI 有清晰包边界；
- 可选能力通过统一扩展缝进入。

它不意味着 Pi 整体功能少。`v0.84.0` 已包含复杂 TUI、树状 session、compaction、多个运行模式、provider/auth、packages、extensions，以及实验性 remote-session client。简单的是**所有权边界**，不是总代码量。[v0.84.0 release](https://github.com/earendil-works/pi/releases/tag/v0.84.0)

## 对 Agent-Smith 的建议顺序

1. **先统一工具契约。** 让 schema、side effect、risk、approval、execution environment、idempotency、concurrency 和 output limit 只有一个来源。
2. **再统一调用 seam。** 所有 direct ReAct、forced skill 和 pipeline node 最终都走同一条 `validate → decide → approve → execute → finalize` 路径。
3. **让 pipeline 只拥有流程。** 它可以决定何时运行哪个 skill/gate，但不要另有工具权限、重试和结果语义。
4. **缩小默认工具面。** 参考 Pi 的四工具默认集，按 identity/skill 临时激活 Web、PDF、Git、memory 等能力；read-only 专用工具可因安全与结构化输出继续保留。
5. **简化 skill 加载。** 评估用受限 `read` 读取已解析、已信任的 skill 路径，逐步替代模型可见的专用加载协议；安装、信任与路径解析仍由 SkillRegistry 管理。
6. **保留 Smith 的差异化能力。** 长期 memory、硬 guard、审批和 sandbox 不应因 Pi 没有就删除。
7. **暂不建设宽 extension 平台，也不急着迁移 session 存储。** 先消除同一决策在多层重复发生，收益更直接、风险更低。

一句话总结：**Pi 证明了单 Agent 内核可以很窄；Smith 应把“工作流丰富”和“内核多套决策”拆开，而不是通过牺牲安全与记忆来换取表面简洁。**
