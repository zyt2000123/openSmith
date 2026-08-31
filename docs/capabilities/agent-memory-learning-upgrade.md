# Smith Memory：持续学习升级方案

> **状态：已于 2026-07-13 被当前记忆实现吸收。** 本文保留为方案与验收背景；当前路径、阈值、文件格式和失败语义以 [21 · 记忆系统](../21-记忆系统.md) 与 `engine/memory/MEMORY_POLICY.md` 为准。

## 1. 目标

Smith 继续是唯一运行中的 Agent。记忆模块在对话后自动提炼对未来有用的信息，使 Smith 能够：

- 逐渐理解用户的稳定偏好和协作方式；
- 延续最近尚未完成的项目工作；
- 复用已经确认的项目事实、决策和解决方法；
- 接受用户纠正，删除过时或错误记忆；
- 在记忆链路失败时仍然正常回答用户。

这不是新增一个“学习 Agent”，也不是保存更多聊天记录。升级重点只有一个：**让现有 Compile 和 Dream 按统一规则生成结构化记忆。**

## 2. 核心设计

所有记忆规则单独定义在 [`engine/memory/MEMORY_POLICY.md`](../../engine/memory/MEMORY_POLICY.md)。

Compiler、Reviewer、Dream 和写入代码必须读取同一份 Policy：

```text
对话与工具结果
  → recent.jsonl（证据）
  → Compiler 按 MemoryPolicy 生成 Markdown 草稿
  → Reviewer 按同一份 MemoryPolicy 审核
  → 代码做安全检查并原子写入
  → Smith 在后续对话中加载或检索正式记忆
```

三个 Markdown 只保存正式记忆，不再包含生成规则、审核说明、原始聊天或模型推理过程。

## 3. 文件职责

| 文件 | 职责 | 正常对话是否读取 |
|---|---|---|
| `SMITH.md` | 用户手写的项目规则；自动学习永不修改 | 固定加载 |
| `engine/memory/MEMORY_POLICY.md` | 三个记忆文件的唯一生成和审核规则 | 仅 Compiler、Reviewer、Dream 读取 |
| `memory/recent.jsonl` | 已清洗的对话、结果和反馈证据 | 不直接注入 Prompt |
| `context.md` | Smith 学到的用户偏好与协作共识 | 每次加载 |
| `memory/recent.md` | 当前项目近期需要延续的工作 | 非空且未过期时加载 |
| `memory/durable.md` | 当前项目长期有效的事实、决策和经验 | 与当前问题相关时加载 |
| `memory/episodes/*.md` | 可检索的任务经历 | 查询命中时加载 |
| `memory/memory_history.jsonl` | 每次编译、审核和写入结果的审计日志 | 不直接注入 Prompt |

正式记忆只有 `context.md`、`recent.md` 和 `durable.md`，不再引入额外状态库、待审队列或第二套记忆数据库。

## 4. 自动学习流程

### 4.1 记录证据

对话结束后，`agent_loop` 仍然只触发一次记忆收尾。满足任一条件时写入 `recent.jsonl`：

1. 本轮使用过工具，产生了可验证的工作结果；
2. 用户明确表达了偏好、纠正、决定、记住或忘记；
3. `UserPreferenceLearner` 发现同一稳定模式已达到置信门槛。

普通闲聊、一次性问答和无未来价值的内容不写入。旧事件格式继续兼容；新事件只增加可选的 `kind`、`scope` 和 `evidence` 字段，不改变现有行偏移语义。

### 4.2 Compiler 生成草稿

Compiler 使用较快、成本较低的模型。每次只编译一个目标文件，并接收：

- 目标文件对应的 MemoryPolicy；
- 当前 Markdown 内容；
- 与目标相关的已清洗事件；
- 当前时间和项目范围。

Compiler 输出完整 Markdown 草稿，必须使用 Policy 规定的标题、章节和条目格式。它要更新、合并和删除旧条目，不能只在文件末尾追加新文本。

### 4.3 Reviewer 审核草稿

Reviewer 使用能力更强的模型，同时读取 Policy、证据、旧文件和新草稿，检查：

- 每条记忆是否有证据支持；
- 是否对未来任务有用；
- 是否写入正确的文件和章节；
- 是否与已有记忆冲突、重复或已经过时；
- 是否满足结构、篇幅、安全和隐私规则。

Reviewer 通过后才能写入。未通过时把问题反馈给 Compiler，沿用现有最多三轮的生成—审核机制；最终仍不通过则保留旧文件，等待下一次编译。

### 4.4 代码写入

Markdown 的内容由模型生成和审核，实际文件 I/O 由确定性代码完成。代码只检查：

- Reviewer 是否明确通过；
- 标题、章节和字符预算是否符合 Policy；
- 是否含密钥或提示词注入内容；
- 写入路径是否合法；
- 原子写入和备份是否成功。

这些是技术校验，不是第三个语义 Gate。任何一步失败都保留旧文件，并写入 `memory_history.jsonl`。

### 4.5 后续召回

| 记忆 | 加载方式 |
|---|---|
| `context.md` | 每次固定加载，保持很小 |
| `recent.md` | 当前项目匹配、内容非空且仍在 3–7 天窗口内时加载 |
| `durable.md`、episodes | 根据当前问题检索命中后加载，受 token 预算限制 |

未通过的草稿、完整 JSONL 和审核过程永不进入正常回答 Prompt。

## 5. 三个文件各自回答什么

| 文件 | 只回答一个问题 | 不得包含 |
|---|---|---|
| `context.md` | “Smith 应该怎样与这个用户协作？” | 项目进度、工具结果、临时任务 |
| `recent.md` | “这个项目最近在做什么，下一步是什么？” | 长期用户偏好、完整回答、原始日志 |
| `durable.md` | “这个项目长期确认了什么，以后可以复用什么？” | 未确认推测、短期待办、普通闲聊 |

三个文件的固定章节、条目模板、准入门槛、冲突规则和 Dream 规则全部放在 MemoryPolicy 中，不在这里重复。

## 6. 保持现有 Compile 与 Dream 原则

| 能力 | 保持不变的语义 |
|---|---|
| `compile_recent()` | 使用完整 3 天窗口，无内容时回退到 7 天；每次重建；窗口为空时清除旧 `recent.md` |
| `compile_durable()` | 继续使用 `.durable_offset` 增量合并新事件与旧 `durable.md` |
| `_generate_and_review()` | 继续执行生成、审核、反馈重试；未通过不得写入 |
| `run_dream()` | 继续低频清秘、去重、压缩和移除过时内容；只整理，不凭空增加事实 |
| 编译与 Dream 计数器 | 仅在对应维护成功后归零；失败后下一轮继续重试 |
| 记忆异常 | best-effort，不阻塞或改变当前对话结果 |

这次升级不同时更换 Cursor、不引入向量数据库，也不重写检索系统。

## 7. 与现有模块的交互

```text
agent_loop
  └─ 对话结束：提交本轮消息、结果、工具使用和学习信号

engine/memory
  ├─ store：记录证据并调度 Compile / Dream
  ├─ compile：加载 Policy，生成并审核三个结构化视图
  ├─ user_learner：只产生偏好信号，不再自行决定 Markdown 排版
  ├─ dream：按 Policy 整理 durable，返回报告
  └─ search：按当前问题检索 durable / episodes

PromptAssembler
  └─ 只接收已经生成好的记忆文本，不了解编译和审核细节
```

因此记忆模块仍是一个独立白盒：输入是事件，输出是三个可读 Markdown 和一份审计日志；外部调用方不需要知道模型、规则模板或文件写入细节。

## 8. 实施顺序

1. **Policy 落地**：增加 Policy loader 和结构校验测试，现有行为不变。
2. **结构化 Compile**：让 recent、durable 和 Dream 共用 Policy；保留当前触发、offset、重试和备份语义。
3. **统一 context 写入**：把 `UserPreferenceLearner` 的结果变成证据，由同一 Compiler/Reviewer 管线生成 `context.md`。
4. **补齐纯聊天信号**：允许明确偏好、纠正、记住和忘记在无工具调用时进入证据日志。
5. **调整召回**：固定加载 context，条件加载 recent，按需检索 durable/episodes。

旧 Markdown 不做一次性破坏性迁移；下一次成功编译时自然重写为新结构。每一步都可以独立回退到上一版文件。

## 9. 验收标准

1. 用户明确要求中文后，下一次会话的 `context.md` 能稳定影响回答。
2. 用户纠正旧偏好时更新原条目，不同时保留互相矛盾的两条记忆。
3. 工具任务结束后，`recent.md` 只记录状态、下一步和已验证结果，不复制完整回答。
4. 3–7 天没有有效事件时，旧 `recent.md` 被清空。
5. `durable.md` 只包含确认事实、决策、可复用流程和已验证陷阱。
6. Reviewer 拒绝、模型超时或写入失败时，旧记忆保留且当前对话正常结束。
7. Dream 不增加证据中不存在的新事实。
8. `SMITH.md` 永远不被自动学习修改。
9. 三个 Markdown 均严格符合 MemoryPolicy 的固定结构和字符预算。

## 10. 非目标

- 不训练或微调模型权重；
- 不新增第二个 Agent 或人工审核流程；
- 不把所有聊天都保存为长期记忆；
- 不把记忆当成工具权限或安全授权；
- 不在本次升级中设计通用插件框架、向量库或新状态机。
