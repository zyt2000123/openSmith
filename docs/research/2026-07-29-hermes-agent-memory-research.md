# Hermes Agent 记忆机制调研与 Agent-Smith 借鉴边界

> 调研日期：2026-07-29
> 范围：只核对 Nous Research 官方 `hermes-agent` 仓库、其随仓文档和源码；本文是设计输入，不是 Agent-Smith 当前实现规格。
> 名称消歧：这里的 “Hermes/Hermess” 指 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，而不是 Hermes 系列模型或其他同名项目。项目既有 [竞品文档](../project/52-竞品对比.md) 也以 “Hermes Agent” 指向该项目。
>
> 实现注（2026-07-29）：本文的 P0 已采用为 Engine-owned 的 `memory/nudge.py`，每 20 个有效记忆回合受限地产生候选；为保持 `memory_ops` 对主 Agent 隐藏，候选不会通过该工具调用。当前规范以 [`21-记忆系统.md`](../subsystems/21-记忆系统.md) 与 [`MEMORY_POLICY.md`](../../engine/memory/MEMORY_POLICY.md) 为准。

## 结论先行

Hermes 最值得借鉴的不是把 Agent-Smith 改成“模型可直接写长期记忆”，而是补齐四个产品/运行时环节：

1. **周期性提醒只产出候选，而不直接落 durable**：Hermes 的内建记忆由 Agent 主动调用工具维护，README 称其有 periodic nudges；Agent-Smith 可以借鉴“提醒”，但必须让提醒产出的仍是带证据的 `memory_ops.add` 候选，由现有 admission、编译和 reviewer 决定是否写入。
2. **把写入结果和容量显式反馈给 Agent/用户**：Hermes 每次写入即时返回成功、重复、超限或匹配歧义；Agent-Smith 应把“候选数、被拒原因、上次 durable 编译、下次维护状态”作为可观察状态，而不是只有后台日志。
3. **把历史检索做成可解释的会话浏览能力**：Hermes 用 SQLite FTS5 搜全量会话，并支持命中窗口/前后滚动。Agent-Smith 已有 episode FTS5，但尚不是完整会话的可审计检索入口；可以在保留 scope、权限和敏感信息过滤的前提下补这一层。
4. **若以后接外部记忆，先定义窄接口和生命周期**：Hermes 的 provider contract 值得参考；不要先接某一家向量库或云服务。

不建议照搬 Hermes 的“Agent 直接维护两份平面 Markdown”作为 Agent-Smith 的 durable 写入路径。它在简洁和响应速度上很好，但会削弱 Agent-Smith 已有的证据、类型、审核、回退和审计闭环。

## 经核实的 Hermes 机制

### 1. 内建长期记忆：两份 profile 级文件，由 Agent 直接维护

Hermes 的内建记忆是 `$HERMES_HOME/memories/MEMORY.md` 与 `USER.md`：前者放环境、项目约定和经验，后者放用户偏好与沟通方式。源码将它们描述为 profile-scoped 存储；条目以 `§` 分隔，默认容量分别为 2,200 与 1,375 个字符，均可由配置覆盖。Agent 通过单个 `memory` 工具执行 `add`、`replace`、`remove`，而不是经过独立的编译器。

- 官方文档：[Persistent Memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md)
- 实现：[tools/memory_tool.py](https://github.com/NousResearch/hermes-agent/blob/main/tools/memory_tool.py)

两个重要细节：

- **冻结快照**：会话开始时装入系统提示词；本会话内写入立即落盘，但不回注入当前 system prompt，下一会话才刷新。这是为保持 prefix cache 稳定而做的取舍。
- **满额不静默压缩**：超出字符上限时，工具返回错误和当前条目，要求 Agent 在同一轮显式合并/删除后重试；没有后台自动删改。

这一路径也不是无防护的：写入前扫描注入/外泄模式，读入 prompt 前再次替换危险条目，写入采用锁与原子替换，并拒绝可能造成外部修改丢失的覆盖。

### 2. “nudge”是促使 Agent 主动复盘，不是确定性的自动提炼

官方 README 明确把机制描述为 “agent-curated memory with periodic nudges”。初始化源码读取 `memory.nudge_interval`，默认值为 10。现有公开源码足以证明存在可配置的周期提醒入口；但本次没有把它表述为“每十轮必定自动写出一条事实”——因为实际持久化仍取决于 Agent 是否调用 `memory` 工具以及工具校验是否通过。

- [README 的闭环说明](https://github.com/NousResearch/hermes-agent/blob/main/README.md)
- [nudge 配置与内建 store 初始化](https://github.com/NousResearch/hermes-agent/blob/main/agent/agent_init.py)

这正好解释了它和 Agent-Smith 的差异：**nudge 是提高候选产生率的行为提示，不是事实可信度证明。**

### 3. 全会话历史与长期记忆分开：FTS5 检索，不靠 LLM 摘要

Hermes 把所有 CLI/消息会话放入 `~/.hermes/state.db`，暴露 `session_search`：

- 以 FTS5 查找命中；
- 返回真实消息，而不是二次 LLM 摘要；
- 支持发现、围绕命中滚动、按时间浏览三种形态。

因此，`MEMORY.md` / `USER.md` 只承载“始终值得进 prompt 的少量内容”，历史细节走按需检索。官方文档明确把两者的容量、成本和用途分开说明。

- [memory 文档中的 Session Search](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md)
- [sessions 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md)

### 4. 外部记忆是可选加层，不替换内建记忆

Hermes 允许同时保留内建文件并启用 **至多一个**外部 memory provider。官方文档列出的生命周期是：向 system prompt 注入 provider 状态、每轮前后台预取、每轮后同步、会话结束提取、镜像内建写入以及暴露 provider 专属工具。

`MemoryProvider` 抽象将 `prefetch`、`queue_prefetch`、`sync_turn`、`on_session_end`、`on_pre_compress`、`on_memory_write`、`on_delegation` 等拆成接口。它说明了可扩展的接缝，但不意味着某一个 provider 的语义、隐私边界或质量能自动适配 Agent-Smith。

- [Memory Providers 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md)
- [MemoryProvider 抽象](https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_provider.py)
- [MemoryManager](https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_manager.py)

## 与当前 Agent-Smith 的对照

| 维度 | Hermes（已核实） | Agent-Smith（当前源码） | 判断 |
| --- | --- | --- | --- |
| 写入入口 | Agent 直接 CRUD 两份记忆文件 | 有工具工作或学习信号才写 `recent.jsonl`；再按类型/范围编译 | Smith 的 admission 更可靠，但候选产出容易偏少 |
| 长期写入 | 直接成功即持久化，靠字符上限和 Agent 自我合并 | durable 仅接收 `correction/decision/remember/forget/verified_fact/procedure/pitfall`，经生成、校验、review/fallback 后提交 | 不应把 Smith 降级成直接自由写 Markdown |
| 定期机制 | nudge 提醒 Agent 主动评估是否要记 | 每个有效 memory 事件计数；满 5 触发编译，满 50 触发 Dream | Smith 的维护可恢复、可审计；需要更好的“何时产生候选”反馈 |
| 常驻/按需 | 两份小型 frozen snapshot + 全会话 FTS5 | `recent.md`/`durable.md` 作为 reference 层；durable 与 episodes 可按需召回 | Smith 已有分层，缺的是会话级可解释召回 |
| 检索索引 | 全量会话 FTS5、命中窗口与滚动 | episode 的 SQLite FTS5（三元分词，损坏可重建），检索命中后注入受字符上限约束 | 不要重复造 episode FTS；应评估是否扩展其数据源和交互形态 |
| 外部后端 | 一个可选 provider 的明确 lifecycle | 当前内建管线 | 仅在明确需要外部/团队共享记忆时引入可插拔 port |

Agent-Smith 的依据：

- [`save_conversation_memory()`](../../engine/memory/store.py) 只为工具工作、显式信号或稳定信号生成事件，并在同一入口独立推进 compile 与 Dream 计数器。
- [`_entries_for_view()`](../../engine/memory/compile.py) 与 [`DURABLE_MEMORY_KINDS`](../../engine/memory/policy.py) 对 durable 做类型和 scope 筛选；[`compile_durable()`](../../engine/memory/compile.py) 才生成并提交文档。
- [`retrieve_relevant_memory()`](../../engine/memory/store.py) 将 recent 设为被动工作层，durable/episodes 走按需召回；`SearchIndex`（`engine/memory/search.py`）当时是可恢复的 SQLite FTS5 episode 索引。
> **该层已不存在**：2026-08-08 的 `b71be4b` 把记忆坍缩为两个整体注入的视图，
> 一并移除了 episode 层与检索索引。当前实现见 [21 · 记忆系统](../subsystems/21-记忆系统.md)。
- [`PromptAssembler.build_layers()`](../../engine/context/assembler.py) 把 memory 标成低权限的 reference，而不是可执行指令。

## 建议的最小借鉴方案

### P0：增加“候选生成 nudge”，不改变 durable admission

在每 N 个**已完成且带工具证据**的事件、或任务完成/失败收尾时，给主 Agent 一个短提醒：只检查是否有 `decision`、`verified_fact`、`procedure`、`pitfall` 或用户偏好可写成带证据的候选。提醒的唯一允许副作用是调用 `memory_ops.add`；不能直接改 `durable.md`。

验收点：

- 没有候选时无写入；
- 普通 `work` 不会因次数自动升格；
- 所有候选仍经过现有类型、scope、secret/injection、编译和 reviewer 闸门；
- UI/API 能显示本次 “nudge → 候选/无候选/拒绝原因”。

这借鉴 Hermes 的积极性，但保持 Smith 的可信度模型。

### P1：做“写入与维护可观察性”闭环

给记忆状态增加用户可读投影，例如：最近一次事件、compile/Dream 的 `idle/running/pending`、durable 候选数、最后一次写入/拒绝原因、各视图字节或 token 预算。这比额外自动写入更能解决“为什么 durable 是空的”的可解释性问题。

Hermes 的容量错误、即时工具结果和 memory 状态界面说明：**容量与失败原因是记忆系统的产品状态，不应只藏在日志里。**

### P1：扩展为安全的“会话历史检索”而非复制一套向量库

以 Agent-Smith 已有 `SearchIndex` 为基础，先设计只读的三态交互：

1. `discover(query)`：返回命中、session/episode provenance、摘要窗口；
2. `scroll(id, cursor)`：只返回有限前后文；
3. `browse(scope, time_range)`：列最近任务记录。

前提是先规定访问控制和清洗：不同用户/项目不能混检；工具原始输出、secret、prompt 指令不得直接回传；召回结果继续标记为不可信 reference。不要因为 Hermes 使用全会话 FTS5 就把所有原始对话自动塞回 prompt。

### P2：若引入外部后端，先抽一个小而严格的 port

只有在出现“跨设备、多人、超出本地 episode 容量、企业知识库复用”等真实需求时，才考虑类似：

```text
prefetch(query, scope) -> recalled references
record_completed_turn(evidence) -> async/bounded sync
on_session_end(...) -> optional candidate extraction
health/status() -> observable state
```

port 的返回必须仍走 Smith 的 reference fencing、scope 隔离和 durable admission；provider 不应取得直接覆盖 `durable.md` 的权限。优先本地/可审计实现，再讨论云端 provider。

## 明确不建议照搬的部分

1. **不让 Agent 直接把任意“学到的东西”写进 durable**。Hermes 的平面文件可用，但不提供 Smith 当前的类型化证据升级与编译审核。
2. **不只靠新会话刷新记忆**。Hermes 的 frozen snapshot 是 prefix-cache 优化；Agent-Smith 的产品语义应继续明确“何时会在下一次 run 生效”，并以检索补足中途需要的历史。
3. **不把外部 provider 当作自动正确性来源**。`sync_turn` 传的是完整对话，成本、隐私、注入与错误提取都必须有独立验收。
4. **不因有 5/50 次计数就承诺自动增长 durable**。计数应驱动维护；候选、证据和审核才驱动长期事实。

## 下一步决策问题

在实现前需要先确认两项产品选择：

1. 对用户而言，“全局记忆”是否应只包含 `USER`/偏好，还是也应包含跨项目可复用 procedure/pitfall？项目事实应如何隔离与撤销？
2. 需要的是 Agent 内部自动召回，还是用户也需要可见、可搜索、可纠正的记忆浏览界面？后者决定是否优先做 discover/scroll/browse。

在这两个问题明确前，P0 的候选 nudge + P1 的状态可观测性是低风险、与现有架构最一致的起点。
