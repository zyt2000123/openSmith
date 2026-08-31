# 主流 Agent 设计调研：从 Claude Code、OpenAI、Anthropic、Google 到 LangGraph

> 调研日期：2026-07-10
> 范围：Agent loop、模型适配、工具、上下文、记忆、权限与沙箱、规划与工作流、人工审批、可观测性与评测、多 Agent。
> 方法：只引用官方文档、官方源码/仓库和一手研究。`zyt2000123/claude-code` 单独作为“第三方镜像快照”分析，不把它当成 Anthropic 官方仓库。

## 一、先给结论

主流 Agent 并不是“一个很长的 System Prompt”，而是一套有明确边界的运行时：

```text
Agent Definition
  = Model + Instructions + Tools + Policies
                    │
                    ▼
Runner / Agent Loop
  model → tool calls → permission/approval → execution → observations → model
                    │
          ┌─────────┼──────────┐
          ▼         ▼          ▼
      Context     Memory    Trace / Eval
                    │
                    ▼
          Workflow / Human / Multi-agent
```

不同厂商的实现细节不同，但它们正在收敛到下面几条原则：

1. **先用最简单的单 Agent 或确定性工作流，只有指标证明需要时才增加自治和多 Agent。**
2. **Agent loop 必须是受控状态机**，需要显式结束条件、最大轮数、预算、取消、重试、暂停与恢复，而不是无限 `while`。
3. **工具接口是 Agent 的真正产品接口。** Schema、描述、错误、权限语义、幂等性和可观测性往往比“再写一层规划 Prompt”更重要。
4. **上下文、会话记忆和长期记忆是三种不同的东西。** 上下文是当前推理工作集，会话记忆是对话状态，长期记忆是跨会话的可审计知识。
5. **行为指导和安全强制必须分层。** Prompt/项目说明只能指导；权限规则、审批、Hook 和 Sandbox 才能强制。
6. **稳定流程由代码编排，开放问题由模型决策。** 成熟系统通常采用二者混合，而不是全靠模型或全靠流程图。
7. **人工审批是可持久化的暂停/恢复协议**，不是让模型在文本里问一句“可以吗”。
8. **Trace 与 Eval 是 Agent 架构的一部分。** 不仅评最终答案，还要评工具轨迹、权限决策、成本、延迟和失败恢复。
9. **多 Agent 的首要价值是上下文隔离、专业化和真正可并行的工作；它不是默认架构。**

## 二、主流系统怎么设计

### 1. Agent loop：最小核心，但必须可控

Anthropic 对 client tool 的标准循环描述非常直接：模型返回 `tool_use`，应用执行工具并回传 `tool_result`，重复直到 stop reason 不再是 `tool_use`。[Anthropic：How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)

OpenAI Agents SDK 的 `Runner` 也是同一个状态机：模型产生最终输出则结束，产生 handoff 则切换 Agent，产生 tool calls 则执行后进入下一轮，并用 `max_turns` 阻止失控循环。[OpenAI Agents SDK：Running agents](https://openai.github.io/openai-agents-python/running_agents/)

因此，一个生产级 loop 至少要有：

- 明确终态：final output、人工中断、取消、失败、预算耗尽、最大轮数；
- 每轮状态：当前 Agent、消息、工具结果、usage、权限决策、重试次数；
- 失败语义：工具失败要作为可理解的 observation 回给模型，而不是吞掉异常；
- 可恢复状态：暂停审批、进程重启或长任务恢复时，不能从头重跑所有副作用；
- 防循环措施：最大轮数、重复调用检测、预算阈值、无进展检测。

**设计判断：** ReAct/工具循环本身不需要过度抽象；真正困难的是终止、恢复、副作用和状态一致性。

### 2. 模型与 Provider：要有边界，但不能抹平能力差异

OpenAI Agents SDK 提供 run 级 `ModelProvider`、Agent 级 `model` 和多 Provider 路由，同时明确提醒：不同 API shape 支持的工具和特性不同，混用时必须确认能力交集。[OpenAI Agents SDK：Models](https://openai.github.io/openai-agents-python/models/)

Google ADK 同样把“多模型生态”列为核心能力，但它的 Orchestration、Tool、Session 并不因此依赖某一个模型实现。[Google：ADK announcement](https://developers.googleblog.com/agent-development-kit-easy-to-build-multi-agent-applications/)

合理的 Provider 边界应负责：

- 消息与工具 Schema 转换；
- streaming、usage、reasoning、stop reason 的规范化；
- 模型能力声明，例如并行工具、结构化输出、图像、prompt cache、server tools；
- provider 特有错误、重试和限流；
- 会话继续机制，如 response ID 或本地完整历史。

不应该假设所有模型都具备完全相同的能力。Provider 接口应该支持 **capability negotiation**，而不是用最低共同标准把高级能力全部丢掉。

还要区分两件事：

- `ModelProvider`：调用底层模型 API；
- `ExternalAgentAdapter`：调用 Codex、Claude Code 这类已经自带 loop、工具和权限的完整 Agent。

后者是更高一层的委派能力，只有产品真的需要“Agent 调 Agent”时才值得建设。

### 3. 工具：把每个工具当成受治理 API

Anthropic 把 tool use 定义成模型与应用之间的契约：模型只输出结构化请求，实际操作由应用或服务端执行。[Anthropic：How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)

Anthropic 的实践总结进一步强调 ACI（Agent-Computer Interface）：工具描述、参数格式、示例和反馈质量直接决定 Agent 表现；应认真测试工具接口，而不是只优化 Prompt。[Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

成熟工具契约通常包含：

- 严格输入/输出 Schema；
- 清晰的“一件事”语义和可操作错误；
- `read_only`、`destructive`、`open_world`、`requires_approval` 等风险属性；
- `concurrency_safe` 与幂等性声明；
- timeout、cancel、retry 以及副作用标识；
- 结果大小限制、落盘/摘要策略和敏感信息清理；
- before/after Hook 与 trace span；
- 大工具集的延迟加载或 Tool Search。

Anthropic 官方文档指出，工具定义和累积的 `tool_result` 都会侵占上下文；可组合使用 Tool Search、programmatic tool calling、prompt caching 和 context editing。[Anthropic：Manage tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)

**设计判断：** 工具注册表不应该只是 `name -> function`；它应该同时是能力目录、风险目录和观测目录。

### 4. Context 与 Prompt：分层、按需加载、可压缩

Claude Code 将项目指导、用户指导、路径规则、Skill 和自动记忆拆成不同层级；项目说明属于上下文，不是强制策略。官方建议 `CLAUDE.md` 保持具体、简洁，并将只对特定路径或任务有意义的内容按需加载。[Claude Code：Memory / CLAUDE.md](https://code.claude.com/docs/en/memory)

Codex 也采用分层指导：全局 `AGENTS.md`、从项目根到当前目录的局部文件按顺序拼接，越靠近当前目录的指导越晚进入上下文；同时为组合大小设置上限。[Codex：AGENTS.md](https://developers.openai.com/codex/guides/agents-md)

推荐把 prompt/context 分成：

```text
稳定前缀：身份、原则、工具核心 Schema、组织策略
项目层：架构、命令、约定、当前目录规则
任务层：用户目标、约束、验收标准
运行层：当前计划、工具 observation、错误和进度
按需层：Skill 正文、知识库片段、长文件、冷门工具
```

关键机制包括：

- 稳定前缀利于 prompt cache；
- 大工具集和 Skill 使用 progressive disclosure；
- 长工具输出先落盘，模型只拿摘要和路径；
- compaction 必须保留目标、未完成项、关键决策和副作用记录；
- 不要把所有长期知识自动塞回每轮上下文。

### 5. Memory：短期状态与长期知识必须分开

OpenAI Agents SDK 的 Session 是“会话历史存储”：它让多个 run 共享对话历史，并支持 SQLite、Redis、SQLAlchemy、MongoDB、加密存储等后端。[OpenAI Agents SDK：Sessions](https://openai.github.io/openai-agents-python/sessions/)

Claude Code 则把跨会话知识拆成两类：

- `CLAUDE.md`：人维护的持久指导；
- Auto memory：Agent 从纠正、偏好和项目经验中提炼出的 Markdown 笔记。

其 `MEMORY.md` 只加载前 200 行或 25KB，详细主题文件按需读取；所有文件可被用户审计、编辑或删除。[Claude Code：Auto memory](https://code.claude.com/docs/en/memory#auto-memory)

这说明 Memory 至少应分为：

| 类型 | 生命周期 | 典型内容 | 风险 |
|---|---|---|---|
| Working context | 一次 run/当前窗口 | 工具结果、当前计划 | 膨胀、context rot |
| Session memory | 一个对话/任务 | 消息、checkpoint、审批状态 | 重放副作用、隐私 |
| Durable memory | 跨会话 | 用户偏好、项目事实、经验 | 错误固化、过期、越权写入 |

长期记忆需要来源、时间、作用域、置信度、可删除性和过期策略。自动提炼可以由后台/分叉 Agent 完成，但写入必须限制目录和工具，并且结果必须可审计。

### 6. 权限、Guardrail、人工审批与 Sandbox：四层防线

这四个概念不能合并成一个 `ToolGuard`：

1. **Permission policy**：当前主体是否有权调用某工具/路径/命令；
2. **Guardrail/Hook**：对输入、输出或单次工具调用做动态检查；
3. **Human approval**：高风险动作暂停，等待人批准、修改或拒绝；
4. **Sandbox**：即使判断失误，也限制进程实际能访问和破坏的范围。

Claude Code 默认只读，写文件和执行可能修改系统的命令需要权限；还提供文件系统/网络隔离的 sandbox，并明确把来自网络的内容视为 prompt injection 风险。[Claude Code：Security](https://code.claude.com/docs/en/security)

Codex 同样把 `approval_policy` 和 `sandbox_mode` 分开：一个控制何时暂停询问，一个控制命令能访问多少文件系统和网络；组织还可禁止危险配置。[Codex：Config basics](https://developers.openai.com/codex/config-basic)

OpenAI Agents SDK 区分 workflow 边界 guardrail 和每次函数工具调用都执行的 tool guardrail，并提醒并行 guardrail 可能在判定前已经消耗 token 或执行工具；高风险场景要使用阻塞检查。[OpenAI Agents SDK：Guardrails](https://openai.github.io/openai-agents-python/guardrails/)

Claude Code 的 `PreToolUse` Hook 可以 allow、deny、ask、defer，也可以在执行前修改参数；这属于机械强制层，而不是 Prompt 建议。[Claude Code：Hooks](https://code.claude.com/docs/en/hooks#pretooluse)

OpenAI 的 HITL 则把审批建模为 interruption：保存 `RunState`，审批或拒绝具体 call ID，再恢复原 run。[OpenAI Agents SDK：Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)

**设计判断：** 审批一定要绑定结构化工具调用和状态快照；Sandbox 仍然不可替代，因为 permission classifier 和 Prompt 都可能出错。

### 7. Planning 与 Workflow：确定性和自治的混合

Anthropic 首先区分 workflow 与 agent：

- workflow：代码预先规定执行路径；
- agent：模型动态决定过程和工具。

其建议是先采用最简单方案；定义清楚的任务优先 workflow，开放问题才使用 agent，因为更高自治通常增加延迟和成本。[Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

OpenAI 也明确给出两种 Orchestration：模型决策或代码编排。代码编排更可预测，适合分类、链式流程、evaluator-optimizer 和真正独立任务的并行；模型编排适合开放任务、动态工具选择和 handoff。[OpenAI Agents SDK：Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)

Google ADK 直接把两类能力都做成产品原语：`Sequential`、`Parallel`、`Loop` 提供可预测流程，`LlmAgent` transfer 提供动态路由。[Google：ADK announcement](https://developers.googleblog.com/agent-development-kit-easy-to-build-multi-agent-applications/)

推荐决策表：

| 情况 | 优先方式 |
|---|---|
| 步骤固定、合规要求高、失败代价大 | 代码 workflow |
| 输入开放、路径不可预先枚举 | 模型驱动 Agent |
| 需要先分类再执行 | 模型输出结构化路由，代码决定后续 |
| 需要反复改进直到达标 | 执行 Agent + evaluator + 最大迭代数 |
| 多个独立信息源 | 并行 fan-out，代码 gather |
| 有不可逆副作用 | 在副作用前设置显式 approval/checkpoint |

### 8. 可观测性与 Evals：必须看到过程，不只看到答案

OpenAI Agents SDK 默认跟踪整个 run、Agent、模型 generation、工具调用、guardrail 和 handoff，并允许用 workflow、trace、group ID 关联执行。[OpenAI Agents SDK：Tracing](https://openai.github.io/openai-agents-python/tracing/)

Google ADK 将本地逐步检查 event/state 和“最终答案 + 执行轨迹”评测都列为核心能力。[Google：ADK announcement](https://developers.googleblog.com/agent-development-kit-easy-to-build-multi-agent-applications/)

Anthropic 的 Agent eval 指南指出，多轮、会调用工具、会改变状态的 Agent 比单次模型输出更难评，需要组合多种 grader。[Anthropic：Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

最小观测模型应该记录：

- run/turn/tool call 的层级 trace；
- 输入输出摘要、耗时、token、缓存命中和成本；
- 权限来源、人工等待时间和批准/拒绝原因；
- 工具失败、重试、取消、超时；
- compaction 前后和恢复 checkpoint；
- 最终验收结果及证据。

Eval 应至少覆盖：

- **Outcome**：任务是否真的完成；
- **Trajectory**：是否选择正确工具、顺序是否合理；
- **Safety**：是否越权、是否在高风险点询问；
- **Efficiency**：轮数、token、延迟、重复调用；
- **Recovery**：工具失败、审批中断和进程重启后能否继续。

### 9. Multi-agent：只在形状合适时使用

Claude Code 官方建议：subagent 适合自包含、输出嘈杂、需要专用工具限制或专业 Prompt 的任务；如果多个阶段共享大量上下文、需要频繁互动或只是小改动，主对话更合适。[Claude Code：Subagents](https://code.claude.com/docs/en/sub-agents)

Codex 给出类似判断：subagent 可隔离 context pollution，并行处理探索、测试和总结；但每个 subagent 都产生额外 token，多个 Agent 同时写代码会产生冲突和协调成本。[Codex：Subagents](https://developers.openai.com/codex/multi-agent)

Google Research 对 180 种配置的实验发现：多 Agent 更可能提升可并行任务，但会降低顺序依赖任务的表现；协调开销和错误传播会抵消收益。[Google Research：Towards a science of scaling agent systems](https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/)

因此，多 Agent 的采用条件应是：

- 子任务可以清晰切分并独立验收；
- 子任务不共享高频可变状态；
- 并行节省的时间大于上下文复制和汇总成本；
- 有明确 owner 负责最终答案或合并；
- 写任务有 worktree/文件所有权或其他隔离；
- 每个 Agent 的工具、预算和结束条件受限。

不要因为“角色听起来合理”就创建 planner、researcher、coder、reviewer 四个 Agent。很多场景下，一个 Agent 加确定性测试/评审步骤更可靠。

### 10. Durable execution：长任务必须能暂停、恢复和去重

LangGraph 把 checkpoint 作为运行时基础：每一步保存 graph state，从而支持 human-in-the-loop、会话记忆、time travel 和故障恢复。[LangGraph：Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

对长任务尤其要设计：

- 可序列化 run state；
- 工具调用唯一 ID 和幂等键；
- checkpoint 后再对外宣告完成；
- 恢复时不重复发送邮件、支付、删除或提交；
- 已完成并行分支不因其他分支失败而全部重跑；
- 外部系统状态需要 read-back verification。

## 三、`zyt2000123/claude-code` 到底能说明什么

### 1. 它不是 Anthropic 官方 Claude Code 仓库

该仓库页面明确显示：

- 它是 `ultraworkers/claw-code` 的 fork；
- README 自称是通过 npm source map 暴露获得的 Claude Code source snapshot；
- README 明确声明它不是 Anthropic 官方、未获 Anthropic 维护或背书；
- 当前仓库只有一个 baseline commit。

来源：[仓库主页与 README](https://github.com/zyt2000123/claude-code)，[固定 commit `4b9d30f`](https://github.com/zyt2000123/claude-code/commit/4b9d30f7953273e567a18eb819f4eddd45fcc877)。

所以它可以用于：

- 观察这份快照的模块边界和工程模式；
- 验证某些公开行为背后可能存在的实现结构；
- 学习大型 terminal Agent 的工程复杂度。

但不能用于：

- 代替 Claude Code 官方文档；
- 证明当前线上版本一定仍采用相同实现；
- 把 README 的“泄漏来源”叙述当成已由 Anthropic 确认的事实；
- 直接复制其中可能受版权和许可证限制的源码。

本文只做架构观察，不复制实现代码。官方行为判断以 [Claude Code 官方文档](https://code.claude.com/docs/en/overview) 为准。

### 2. 这份快照呈现出的设计模式

#### 显式状态机式 Agent loop

[`src/query.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/query.ts#L219) 中的 `queryLoop` 是显式 `while (true)`，但其状态包含 turn count、compaction、stop hook、token budget 和 transition reason；这印证“循环简单，状态与终止复杂”。

#### 工具契约远超 name/function

[`src/Tool.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/Tool.ts#L358) 的 Tool 类型包含 Schema、`isConcurrencySafe`、`isReadOnly`、`isDestructive`、`isOpenWorld`、用户交互、deferred loading、结果大小、输入验证和权限检查。

这正是成熟 Agent 工具层与普通函数注册表的差别。

#### 在模型看到工具之前就做权限过滤

[`src/tools.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/tools.ts#L254) 会根据 blanket deny rule 先移除工具，而不是等模型调用后才拒绝；组装 built-in 与 MCP 工具时还保持稳定排序以保护 prompt cache。

这说明安全和 context/cost 优化会进入同一个工具装配管线，但仍需保持概念上的职责分离。

#### 读操作并行，写操作保守串行

[`src/services/tools/toolOrchestration.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/services/tools/toolOrchestration.ts#L19) 根据 `isConcurrencySafe` 将连续工具调用分组：安全组并行，其他调用串行；解析失败或判断异常时保守地视为不安全。

这比“模型一次返回多个 tool call 就全部并行”更适合有状态工具。

#### 长期记忆由受限的分叉 Agent 提炼

[`src/services/extractMemories/extractMemories.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/services/extractMemories/extractMemories.ts#L1) 显示：完整 query loop 结束后，系统可运行一个共享 prompt cache 的 forked agent 提炼 durable memory；该任务不写主 transcript，工具权限限制到 memory 目录，并设置 `maxTurns: 5`。

这是一种值得借鉴的模式：记忆提炼不污染主 loop，但必须限权、限轮次、可审计。

#### 多 Agent 已经带来额外治理复杂度

快照中不仅有 `AgentTool`，还有 coordinator、mailbox、worker permission forwarding、task state 和 worktree 工具。它说明多 Agent 不是“再调用一次模型”，而会引入：

- worker 生命周期和取消；
- 上下文传递与结果汇总；
- 权限请求向 leader 转发；
- 共享任务状态和消息系统；
- 写冲突与工作区隔离；
- 每个 worker 的预算和恢复。

这也是为什么不应把多 Agent 作为 Agent-Smith 的第一优先级。

## 四、对 Agent-Smith 的设计建议

下面先把主流设计轴映射到当前工作树，再给出演进建议。这里区分“已经有结构”与“已经形成完整生产能力”，避免因为类或文件存在就把主路径能力视为完成。

| 设计轴 | Agent-Smith 当前实现 | 判断 |
|---|---|---|
| Agent loop / workflow | [`run_agent_stream`](../../engine/execution/orchestration/agent_loop.py) 负责 Direct/Pipeline 分派；ReAct、Pipeline、Gate 与生命周期已拆入各自子包 | 边界已加深，应继续通过架构测试防止职责回流 |
| Model Provider | [`LLMClient`](../../engine/llm/client.py) 统一调用 OpenAI-compatible Chat Completions，并归一化 tool call 与 usage | 已有窄接口；缺少显式 capability 描述，不宜提前扩成“大一统 Provider” |
| Tool Contract | [`ToolRegistry`](../../engine/tool/registry.py) 当前主要保存 name、description、parameters 和 callable | 能工作但偏浅；风险、并发、审批、执行环境、输出策略仍散落在注册表之外 |
| Context / Memory | [`PromptAssembler`](../../engine/context/assembler.py) 分层组装身份、工作流、工具、Skill、记忆和运行时上下文；[`engine/memory/`](../../engine/memory) 提供 recent 事件源、durable 编译、Dream 与 episode FTS 检索 | 方向符合主流；下一步是明确 context、session、durable memory 的生命周期和评测 |
| Permission / Execution | `ToolPolicy` / `ToolGuard` 在工具前做规则和路径检查，但 [`shell.execute`](../../agents/tools/shell.py) 仍通过宿主机 subprocess 执行 | “是否允许”已有基础，“在哪里执行”仍缺真正的执行环境隔离 |
| Observability / Eval | [`ExecutionEvent`](../../engine/execution/events.py) 定义协议；[`engine/observability/`](../../engine/observability) 持久化脱敏 trace、summary 与诊断投影 | 已有本地可观测性；仍需独立的 outcome/trajectory eval harness |

总体判断：Agent-Smith 已经具备主流 Agent 的核心骨架，真正的差距不在于“再增加几个 Agent”，而在于把 Tool Contract、执行环境、可恢复状态和 Eval 做成深模块。

### P0：先把单 Agent 运行时做深

保留“一个常驻本地 Agent”作为主产品模型，把主循环明确成可测试状态机：

```text
READY
  → MODEL_RUNNING
  → TOOL_PENDING
  → APPROVAL_PENDING（可选）
  → TOOL_RUNNING
  → OBSERVING
  → MODEL_RUNNING
  → COMPLETED / FAILED / CANCELLED / BUDGET_EXCEEDED
```

每次 transition 都产生结构化事件并可 checkpoint。完成判断不能只看模型说“完成了”，还要有验收证据。

### P0：把 Tool Contract 升级为核心接口

建议最少具备：

```text
name / description / input_schema / output_schema
risk: read_only | write | destructive | external_side_effect
concurrency: safe | serial
approval: never | policy | always
environment: host | sandbox | either
timeout / cancel / idempotency_key
result_policy: inline | truncate | persist
```

这样权限、并行、Sandbox、日志和 UI 都能消费同一份元数据。

### P0：拆开“允许做”和“在哪里做”

建议形成：

```text
Tool Policy / Guardrail  → 能不能做
Human Approval           → 是否需要人确认
Execution Environment    → 在哪里执行
Audit / Trace            → 实际发生了什么
```

`LocalExecutionEnvironment` 服务于用户电脑上的真实操作；`SandboxExecutionEnvironment` 服务于运行测试、安装依赖、执行未知项目代码等高不确定任务。两者不应塞进每个 Tool 的实现中。

### P1：把 Context、Session 和 Durable Memory 分开

- Context assembler 只负责本轮模型输入；
- Session store 保存可恢复对话与 checkpoint；
- Durable memory 保存跨会话事实与偏好；
- 项目规范和长期记忆都应支持按路径/主题渐进加载；
- 自动记忆写入要带来源和时间，可由用户审计、修改和删除。

可借鉴镜像快照中的 forked memory extractor，但不要让它拥有任意宿主机工具权限。

### P1：用混合 Orchestration，不建立“万能 Planner”

- 固定步骤由 `SkillChain`/代码 workflow 管；
- 单个开放节点内部让 Agent 自主 ReAct；
- evaluator 只在存在明确 rubric 时使用；
- 所有循环必须有 max iteration 和 no-progress 条件；
- 高风险副作用前插入 checkpoint + approval。

### P1：先建立 Trace/Eval，再扩展模型和 Agent 数量

至少形成一组固定回归任务：文件修改、命令执行、权限拒绝、审批恢复、工具失败恢复、context compaction、记忆召回、Sandbox 越界阻断。每个案例同时检查最终结果和执行轨迹。

如果没有这些指标，增加 Provider、多 Agent 或复杂 Planner 后，很难知道质量是提升还是退化。

### P2：Provider 抽象保持窄而诚实

可以提供 `LLMProvider`，但保留 provider capability 字段。不要为了“以后可能支持所有模型”提前重写完整执行引擎。

只有明确出现“把编码子任务委派给 Codex/Claude Code/Pi”的产品需求时，再新增 `ExternalAgentAdapter`。它的结果应该被当成外部任务产物，需要独立权限、工作区、预算和验收。

### P2：多 Agent 只开放给可并行、可隔离任务

第一批适合的场景：

- 多来源只读调研；
- 独立测试/日志分析；
- Reviewer 对已完成 diff 做只读检查；
- 有独立 worktree 的互不重叠实现任务。

不适合的第一批场景：

- 多个 Agent 同时修改同一工作区；
- 强顺序依赖但仍强行并行；
- 用多个角色替代清晰的工具、rubric 和测试；
- 没有预算、取消和冲突解决机制的“Agent swarm”。

## 五、推荐的演进顺序

```text
1. 单 Agent loop 状态机 + checkpoint
2. Rich Tool Contract
3. Permission / Approval / ExecutionEnvironment 分层
4. Context budget + compaction + progressive disclosure
5. Session 与 Durable Memory 分离
6. Trace + outcome/trajectory eval
7. Sandbox + Git worktree 任务交付
8. 窄 LLMProvider 能力适配
9. 有真实需求后再做 ExternalAgentAdapter / Multi-agent
```

最关键的取舍是：

> Agent-Smith 不需要用“更多 Agent”证明它是 Agent 系统。更有价值的是把一个常驻 Agent 的工具契约、权限边界、记忆、恢复和可验证完成做扎实。

## 六、主要一手来源

### Anthropic / Claude Code

- [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [How tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
- [Manage tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)
- [Claude Code memory and CLAUDE.md](https://code.claude.com/docs/en/memory)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code security](https://code.claude.com/docs/en/security)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

### OpenAI

- [Agents SDK: Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- [Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [Agents SDK: Models](https://openai.github.io/openai-agents-python/models/)
- [Agents SDK: Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- [Agents SDK: Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- [Agents SDK: Sessions](https://openai.github.io/openai-agents-python/sessions/)
- [Agents SDK: Tracing](https://openai.github.io/openai-agents-python/tracing/)
- [Codex: AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- [Codex: Subagents](https://developers.openai.com/codex/multi-agent)
- [Codex: Config basics](https://developers.openai.com/codex/config-basic)

### Google / LangGraph

- [Google ADK announcement](https://developers.googleblog.com/agent-development-kit-easy-to-build-multi-agent-applications/)
- [Google Research: Towards a science of scaling agent systems](https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

### 第三方 Claude Code 镜像（仅用于快照架构观察）

- [`zyt2000123/claude-code` README](https://github.com/zyt2000123/claude-code)
- [固定 baseline commit](https://github.com/zyt2000123/claude-code/commit/4b9d30f7953273e567a18eb819f4eddd45fcc877)
- [`query.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/query.ts)
- [`Tool.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/Tool.ts)
- [`tools.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/tools.ts)
- [`toolOrchestration.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/services/tools/toolOrchestration.ts)
- [`extractMemories.ts`](https://github.com/zyt2000123/claude-code/blob/4b9d30f7953273e567a18eb819f4eddd45fcc877/src/services/extractMemories/extractMemories.ts)
