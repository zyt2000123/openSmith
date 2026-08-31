# Agent memory quality research

> 调研记录，不是当前实现规格。后续设计已决定不引入 candidate 层或人工审核；最终方案见 `docs/subsystems/21-记忆系统.md` 与 `engine/memory/MEMORY_POLICY.md`。

## Executive conclusion

“越用越聪明”不等于把更多聊天追加到 `memory.md`。高质量记忆需要一个闭环：

```text
事件 → 候选记忆 → 证据校验 → 正式记忆 → 按需召回
  ↑                              ↓
  └────── 任务结果 / 用户纠正 ────┘
```

记忆的目标是提高未来任务的成功率，而不是最大化历史文本量。

## 网上方案的共同结论

### 1. 先区分记忆类型

LangGraph 将长期记忆分为 semantic、episodic、procedural：事实、经历、做事规则。它还对 semantic memory 区分了两种组织方式：

- profile：一份持续更新的、范围明确的结构化资料，适合稳定的用户或项目事实；
- collection：许多粒度更小的独立记忆，新增和检索更容易，但需要额外处理更新、删除和去重。

因此，Agent-Smith 不应让所有内容共用一份 Markdown。建议至少使用以下类型：

| 类型 | 应记录的内容 | 默认生命周期 |
| --- | --- | --- |
| `preference` | 用户明确偏好、表达习惯、代码风格 | 长期，可被纠正 |
| `fact` | 经工具、文件、测试或用户确认的稳定事实 | 长期，需带来源 |
| `decision` | 已确认的架构、产品或工作流决策 | 直到被替代 |
| `procedure` | 已验证且可复用的做法 | 长期，需有成功证据 |
| `episode` | 一次任务的结果、关键路径和经验 | 按需检索 |
| `candidate` | 尚未充分确认的候选结论 | 不默认注入 |
| `working` | 当前 Run 的计划、临时状态和工具输出 | 仅当前 Run |

来源：[LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)。

### 2. 按重要性和规模分层

Letta 的 context hierarchy 将记忆分为始终放在上下文中的 memory block、可分段读取的文件、按需查询的 archival memory，以及外部检索库。重要且少量的信息适合常驻；不重要或数量较大的信息应该保留但按需检索。

这说明：`context.md` 不应该成为所有记忆的入口，`recent.md` 也不应该承载完整历史。常驻内容越少，模型越容易遵守；其余内容需要通过查询召回。

来源：[Letta Context Hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)。

### 3. 记录“未来有用的学习”，而不是原始聊天

Claude Code 的 auto memory 明确选择保存纠正、构建命令、调试经验、架构笔记、代码风格偏好和工作习惯，并建议用简短索引指向主题文件，而不是让一个入口文件无限增长。

对 Agent-Smith 来说，值得写入长期记忆的内容包括：

- 用户明确说“记住”的事实、偏好或规则；
- 用户纠正 Agent 后形成的行为规则；
- 多次出现且稳定的偏好；
- 被工具、测试、文件或权威资料验证过的项目事实；
- 已确认的架构决策；
- 已验证成功、下次可复用的解决策略；
- 带适用条件的失败经验。

默认不应直接写入：普通闲聊、一次性问答、当前任务临时状态、原始工具输出、未经验证的模型推测，以及一次失败就总结出的永久规则。

来源：[Claude Code Memory](https://code.claude.com/docs/en/memory)。

### 4. “变聪明”来自反馈闭环

Generative Agents 的研究显示，观察、计划和反思共同影响后续行为；Reflexion 则把任务反馈转换成文字反思，并放入 episodic memory，以影响后续决策。对 Agent-Smith 而言，真正有价值的强化信号应该来自用户纠正、测试结果、工具成功/失败和任务是否完成，而不是单纯的“这条记忆被读取过”。

来源：[Generative Agents](https://arxiv.org/abs/2304.03442)、[Reflexion](https://arxiv.org/abs/2303.11366)。

## 推荐的记录结构

Markdown 可以继续保留，但应该降级为人类可读的派生视图。真实记录至少应能表达：

```json
{
  "id": "mem_123",
  "kind": "decision",
  "scope": "project",
  "claim": "RunState 位于 engine/execution/",
  "status": "active",
  "confidence": 0.95,
  "importance": 0.8,
  "evidence_refs": ["session:...", "user:confirmed"],
  "created_at": "...",
  "updated_at": "...",
  "last_recalled_at": "...",
  "expires_at": null,
  "supersedes": [],
  "contradicts": []
}
```

`confidence`、`importance` 和当前查询的 `relevance` 要分开：被读取很多次只能说明可能常用，不能自动证明它更真实。冲突时不要直接覆盖旧内容，而应保留版本，把旧记录标为 `superseded`，无法判断时标为 `disputed`。

## 对当前 Agent-Smith 的判断

当前实现已经有 `recent.jsonl` 作为事件源，也有编译、Dream 和用户偏好学习。但当前事件主要只有 `task / summary / timestamp`，Markdown 编译层缺少记录级身份、证据和冲突链；本地实际生成的 `recent.md` 已经混入重复事件和长篇回答式文本，而 `durable.md` 仍为空。这正是“内容很多但记忆不聪明”的典型表现。

建议的目录职责：

- `context.md`：只放少量高优先级用户偏好；
- `durable.md`：只渲染 active 的稳定事实、决策和程序性经验；
- `recent.md`：只保留压缩后的近期任务索引，不保存完整回答；
- `episodes/` 或结构化事件库：保存可按需检索的任务经历；
- `candidate` 区：保存待确认内容，默认不注入；
- `recent.jsonl`：继续作为原始事件源，但不再直接等同于正式记忆。

## 建议的实现顺序

1. 先定义 `MemoryRecord` 和 admission policy，明确什么能从事件升级为正式记忆。
2. 让结构化记录成为真实数据源，Markdown 只负责渲染和人工审阅。
3. 把 `preference / fact / decision / procedure / episode` 分开写入和召回。
4. 增加 `active / candidate / superseded / disputed / expired` 状态及证据引用。
5. 支持“记住、确认、忘记、列出记忆”，并允许用户纠正旧记录。
6. 用多会话评测验证：信息提取、时间更新、冲突解决、选择性遗忘、无关记忆不被召回，以及任务成功率是否提升。

第一阶段不需要引入向量数据库；先把记录结构、写入门槛、冲突更新和 Markdown 渲染做好，再决定是否需要更复杂的检索层。
