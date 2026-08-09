# 05 · Engine 记忆系统

## 1. 目标与边界

Smith 是唯一运行中的 Agent。记忆模块把经证据支持、且对未来对话仍有用的内容，编译成两个有界 Markdown 视图：

- `context.md`：用户级协作偏好和稳定背景；
- `memory/durable.md`：项目级工作状态、待处理项、已验证结论、决定和陷阱。

实现不保存完整聊天作为正式记忆，不训练模型权重，不启动第二个 Agent，也没有查询时检索、FTS、向量索引或 episode 层。`SMITH.md` 始终由用户维护，自动记忆永不修改它。

## 2. 端到端闭环

```text
对话终止
  → lifecycle 区分 completed / incomplete / failed
  → store 清洗并追加证据到 recent.jsonl
  → 显式学习信号，或每 5 个有效记忆回合：
      compile_context → compile_durable
      每个视图：Generator → Reviewer → 结构/安全校验 → 原子写入
  → 每 50 个有效记忆回合：Dream 清洗视图并回收已消费的过期证据前缀
  → 下一轮完整加载 context.md 与 durable.md
```

生产 Server 共享 LLM client 时，编译和 Dream 由 `MemoryMaintenanceService` 标记 pending 并在后台运行；当前回答不因记忆维护失败而回滚。

## 3. 文件与状态

所有运行时路径都相对当前 Agent profile：

| 路径 | 职责 | 正常回答是否读取 |
| --- | --- | --- |
| `context.md` | 已接受的用户协作记忆，4K 字符上限 | 是，整份 |
| `memory/durable.md` | 已接受的项目记忆，10K 字符上限 | 是，整份 |
| `memory/recent.jsonl` | 追加式证据日志 | 否 |
| `memory/memory_history.jsonl` | 带哈希、状态、审核轮次和脱敏错误的审计日志 | 否 |
| `memory/.compile_offset` | Compiler 已消费的 JSONL 行数 | 否 |
| `memory/.fp_context` / `.fp_durable` | 视图输入指纹，用于跳过重复编译 | 否 |
| `memory/.compile_counter` / `.dream_counter` | 维护触发计数 | 否 |
| `memory/.compile_pending` / `.dream_pending` | 延迟维护标记 | 否 |
| `memory/.dream_cleanup.json` | JSONL 替换的可恢复 journal | 否 |

`engine/memory/MEMORY_POLICY.md` 是两个视图共享的唯一生成、审核、结构和预算契约。

## 4. 证据记录

`save_conversation_memory()` 在以下情况之一追加证据：

1. 本轮存在真实、成功的工具结果；
2. 用户明确表达偏好、纠正、决定、记住或忘记；
3. `UserPreferenceLearner` 对同一模式累计观察 3 次后产生稳定信号。

无工具、无学习信号的普通聊天不记录。未完成回合使用 `partial_work` 与 `partial_tool_result`，不得被当作已验证成果。

事件基本格式：

```json
{
  "task": "已清洗的用户任务",
  "summary": "已清洗的 Smith 回答",
  "timestamp": "ISO-8601 UTC",
  "kind": "work|partial_work|preference|correction|decision|remember|forget|pattern|verified_fact|procedure|pitfall",
  "scope": "user|project",
  "evidence": "tool_result|partial_tool_result|user_explicit|repeated_observation|test_result|source_document",
  "signals": ["tech_level=expert"]
}
```

单个 task/summary 超过 16K 字符时保留首尾并加明确截断标记。密钥和已知提示词注入行在入库前删除或红线拒绝。文件使用 `0600`，目录使用 `0700`。

## 5. 两个正式视图

| 视图 | 证据筛选 | 更新方式 | 上限 |
| --- | --- | --- | --- |
| `context.md` | `scope=user` 的明确信号与稳定模式 | 完整证据 + 当前文件的审核合并 | 4K |
| `durable.md` | offset 后的项目 work/decision/correction/remember/forget/verified_fact/procedure/pitfall | 新证据 + 当前文件的增量合并 | 10K |

`context.md` 固定包含 `Confirmed Preferences` / `Collaboration Patterns` / `Stable User Context`。

`durable.md` 固定包含 `Active Work` / `Pending` / `Verified Outcomes` / `Decisions` / `Known Pitfalls`。

空章节保留标题。每份文档只能有一个规定的一级标题，二级章节名和顺序必须与 Policy 完全一致，不允许代码围栏。

## 6. Compiler、Reviewer 与写入

Compiler 每次只处理一个目标视图，并接收当前 UTC 时间、目标 Policy、当前已接受 Markdown 和选中证据。Reviewer 同时看到 Policy、旧文档、证据和草稿，最多三轮返回结构化审核结果。

正常写入必须经过：

- Reviewer 通过；
- 目标路径不逃出 Agent profile；
- 标题、章节与字符预算校验；
- 密钥和提示词注入扫描；
- 旧文件 `.bak` 备份与 `os.replace()` 原子替换；
- `memory_history.jsonl` 审计。

`compile_context()` 没有降级写入。`compile_durable()` 的受限 fallback 见下节。

## 7. durable 确定性 fallback

当 Reviewer 已配置，且生成—审核尝试因最终审核未通过或超时而失败时，`durable.md` 可使用一个受限的抽取式 fallback，避免记忆在提供方抖动或接近容量上限时永久停滞。

fallback 的红线：

- 批次中存在 `correction` 或 `forget` 时禁止 fallback；旧文件与证据保留待重试；
- `partial_work` 只写 `Active Work`，不得写 `Verified Outcomes`；
- 自动 `work.summary` 是助手回复，不是原始工具证据；它只能作为 `Pending` 待复核摘要；
- 用户明确的 decision 记录决定本身，不记录助手确认语；
- `memory_ops.add` 候选以 `content` 作为记忆正文，`evidence` 只作支持说明，不得反向固化为事实；
- 已接受旧条目整条保留，不做字符级截断；
- 超出 10K 时，按 `Active Work` → `Pending` → `Verified Outcomes` → `Decisions` → `Known Pitfalls` 的价值顺序，在同一章节内从前到后删除完整条目；
- 输出仍必须通过全部路径、结构、预算和安全校验，并以 `status=fallback` 备份和审计。

## 8. offset、指纹与调度

`run_compilation()` 按 `compile_context → compile_durable` 运行。`durable` 只读取 `.compile_offset` 之后的证据，但合并时会完整携带当前 `durable.md`，因此旧事实不会因时间窗消失。

只有无错且至少一个视图真正处理了输入时，全局 compile offset 才前进。某个视图失败时，其指纹和未消费证据保留供重试。

普通事件每 5 个记忆回合触发 compile；明确学习信号立即触发。Dream 每 50 个记忆回合触发。计数在失败时保持到阈值，但提供方/传输失败使用 10 分钟持久冷却，避免每轮重击 LLM。审核或内容拒绝不进入这个冷却。

同一 memory 目录的维护同时持有进程内 `asyncio.Lock` 与进程间文件锁，避免 compile/Dream 交叉替换文件。

## 9. Dream 卫生与可恢复清理

Dream 不再生成知识或二次改写 durable，只做两件事：

1. 对 `context.md`、`durable.md` 与遗留 episode Markdown 执行确定性密钥/注入清洗；
2. 只删除 Compiler 已消费、且超过 7 天保留期的 `recent.jsonl` 连续前缀。

替换 JSONL 前，Dream 先写 `.dream_cleanup.json`，其中包含旧/新日志 SHA-256 和清理后 compile offset。下次运行先恢复或安全放弃这份 journal，防止已截断日志与 checkpoint 不一致导致证据丢失或重放。

## 10. Prompt 注入

`PromptAssembler` 把 `context.md` 作为 Learned User Context 完整读取。`prepare_runtime()` 通过 `assemble_memory()` 完整读取 `durable.md`，然后作为 Durable Memory 层传入。

两个视图在进入 Prompt 前都会再次清洗，并带有明确安全围栏：记忆是不可信的历史参考，不是指令，不能覆盖 system/developer、`SMITH.md`、当前用户请求或工具权限。

`context.md` 由 4K Policy 上限保持常驻可控；`durable.md` 由 10K 上限约束，并在整体 Prompt 超预算时作为可裁剪的参考层。

## 11. `memory_ops` 工具

`memory_ops` 仅提供：

- `add`：向 `recent.jsonl` 追加结构化候选证据，不直接写正式 Markdown；
- `search`：对完整 durable 视图和最近 JSONL 事件做有界关键词搜索。

`add` 必须提供 `kind`、`scope`、`evidence_type`、`content` 和 `evidence`。`plan` / `task` / `todo` / `task_step` 一律拒绝，它们属于 Todo/session state。该工具在 Prompt 工具目录中隐藏，但运行时会注入 Engine 拥有的路径与安全能力。

## 12. 失败语义与验收

| 失败 | 结果 |
| --- | --- |
| context Reviewer 拒绝/缺失/超时 | 不写 `context.md`，保留证据重试 |
| durable Reviewer 拒绝/超时 | 仅在第 7 节红线内 fallback，否则保留旧视图与证据 |
| correction/forget 审核失败 | 禁止 fallback，不推进 checkpoint |
| Markdown 结构/预算/安全不合规 | 不替换旧文件，写审计 |
| 提供方或传输故障 | 保留 pending/计数，冷却后或 idle tick 重试 |
| Dream 恢复/清理失败 | 不跨过未确认证据，保留 journal 并审计 |
| 记忆收尾失败 | 当前对话结果不回滚 |

验收基线：

1. 明确偏好/纠正/决定/记住/忘记在无工具回合也能进入证据流；
2. 普通纯聊天不写记忆；
3. 两个 Markdown 都符合同一 MemoryPolicy；
4. Reviewer/写入失败不损坏已接受文件；
5. 未完成工作和未审核助手结论不进入 `Verified Outcomes`；
6. 纠正/忘记不会被 fallback 跨过；
7. 近满 durable 的 fallback 仍在 10K 内，且不截断保留条目；
8. 正常回答不读取 `recent.jsonl` 或 `memory_history.jsonl`；
9. `SMITH.md` 不被自动记忆修改；
10. Engine、Server 与 Shell 契约测试通过。

当前实现基线：2026-08-08。规则以 `engine/memory/MEMORY_POLICY.md` 为准，行为以 `engine/tests/memory/` 及 context/execution/server 集成测试为准。
