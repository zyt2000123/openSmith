# 05 · Engine 记忆系统

## 1. 目标与边界

Smith 是唯一运行中的 Agent。记忆模块的目标不是保存更多聊天，而是让 Smith 在后续对话中稳定复用已经确认的用户偏好、近期工作和长期项目共识。

本模块必须同时满足：

- 自动闭环，不依赖人工审核；
- 记忆内容可读、可检查、可删除；
- 编译或审核失败时保留旧记忆，不影响当前回答；
- 常驻上下文保持很小，长期内容按需召回；
- `SMITH.md` 始终由用户维护，自动学习永不修改。

本设计不训练模型权重，不引入第二个运行 Agent，也不把原始聊天日志直接当作正式记忆。

## 2. 一条完整闭环

```text
对话结束
  → agent_loop 提取工具活动和学习信号
  → store 将已清洗证据追加到 recent.jsonl
  → 每 5 个有效记忆回合（或显式学习信号）：Compiler 按 MemoryPolicy 生成完整 Markdown 草稿
      → Reviewer 按同一份 MemoryPolicy、旧文件和证据审核
      → 通过：确定性代码校验、备份并原子替换
      → 拒绝/超时/异常：保留旧文件并记录审计结果
  → 每 20 个有效记忆回合：Nudge 只审阅已完成的工具证据
      → 无候选：记录 nudge=unchanged，不修改正式记忆
      → 有受限候选：追加候选事件，再复用 Compiler → Reviewer → 确定性写入链路
      → 拒绝/超时/异常：保留计数与 checkpoint，记录 nudge 审计后重试
  → 后续对话固定加载 context、加载 recent、按问题召回 durable/episodes
  → 用户纠正、忘记请求和新任务结果再次进入证据流
```

Compiler 和 Reviewer 是两个模型角色。生产运行时 Compiler 使用 `RuntimeServices.background_llm`（未配置时回退 `services.llm`），Reviewer 使用 `RuntimeServices.gate_llm`。

正式 Markdown 的写入有三条路径：Reviewer 明确通过的正常路径；`recent.md`/`durable.md` 在 Compiler 超时或多轮审核仍未通过时的**抽取式 fallback** 路径（`_fallback_recent_document` / `_fallback_durable_document`，不含模型新增内容、只做确定性证据抽取，仍过全部结构/密钥/注入校验，审计 status 为 `fallback`，并推进指纹与 offset）；以及 `durable.md` 首次初始化时写空模板（status 为 `initialized`）。`context.md` 没有 fallback 路径。

写文件的代码只做确定性技术校验，不做第三次语义裁决。

## 3. 文件职责与路径

所有运行时记忆路径都相对当前 Agent profile：

| 文件 | 职责 | 正常回答是否读取 |
|---|---|---|
| `SMITH.md` | 用户手写规则与项目指令 | 固定加载，自动记忆永不写入 |
| `engine/memory/MEMORY_POLICY.md` | 三个正式视图唯一的生成、审核和格式规则 | 仅 Compiler、Reviewer、Dream、Nudge、topic sync 读取（Nudge 的候选审核规则目前硬编码在 `nudge.py`，仅从 Policy 读版本号） |
| `context.md` | 已确认的用户偏好、协作模式和稳定用户背景 | 每轮固定加载 |
| `memory/recent.jsonl` | 追加式、已清洗的证据日志 | 不直接进入回答 Prompt |
| `memory/recent.md` | 最近 3–7 天仍需延续的工作 | 非空时加载 |
| `memory/durable.md` | 稳定项目事实、决定、流程和陷阱 | 仅查询命中时加载 |
| `memory/episodes/*.md` | 可检索的任务经历（含用户 episode、Nudge episode、topic sync 生成页与 `.bak`） | 仅查询命中时加载 |
| `memory/episodes/topic-associations.json` | durable 条目 → topic 页面的路由关联（entry-id、文件名、页面哈希快照） | 不进入回答 Prompt |
| `memory/episodes/vectors.json` | 可选的 topic 页面向量索引（启用 embeddings 时） | 不进入回答 Prompt |
| `memory/episodes/search.sqlite` / `.fts_version` | FTS5 检索索引与 schema 版本 | 不进入回答 Prompt |
| `memory/episodes/.index_state.json` | 逐文件 mtime/size 签名，驱动索引增量重建 | 不进入回答 Prompt |
| `memory/memory_history.jsonl` | 编译、审核和写入审计（Dream 定期裁剪到 500 条 / 90 天） | 不进入回答 Prompt |
| `memory/.nudge_counter` / `.nudge_offset` | 20 回合质量检查的待触发计数与已审阅证据 checkpoint | 不进入回答 Prompt |
| `memory/.{compile,nudge,dream}_pending` | 延迟运行时尚未完成的对应 lane 标记 | 不进入回答 Prompt |
| `memory/.{compile,nudge,dream,topic_sync}_retry_attempt` | 传输/provider 失败后的 600 秒重试冷却标记 | 不进入回答 Prompt |
| `memory/.maintenance` | 维护任务的进程间锁文件 | 不进入回答 Prompt |

`context.md` 位于 profile 根目录，是因为它属于 Smith 与用户之间的常驻协作上下文；项目记忆位于 `memory/`，便于独立维护、清理和检索。Policy 位于 Python 包内并作为 package data 发布，确保源码运行和 wheel 安装使用同一份规则。

## 4. 证据写入

### 4.1 何时记录

`save_conversation_memory()` 满足任一条件时才写证据：

1. 本轮实际调用了工具或技能；
2. 用户明确表达偏好、纠正、决定、记住或忘记；
3. `UserPreferenceLearner` 对同一模式累计观察达到 3 次。

普通闲聊、一次性问答和没有未来价值的纯聊天不会进入证据日志。

明确学习信号会立即触发一次编译；普通工具工作仍沿用每 5 个有效 turn 编译一次。写入失败时，学习器不会确认信号，下次观察会继续重试。

同一类有效记忆回合每累计 20 次还会触发一次 Nudge。它只从已完成的 `work/tool_result` 证据中寻找可复用的项目结论；空候选是正常结果，候选本身也不能直接改写 `durable.md`。

### 4.2 事件格式

旧的三字段事件继续兼容。新事件增加可选分类字段：

```json
{
  "task": "用户消息的已清洗文本",
  "summary": "Smith 回答的已清洗文本",
  "timestamp": "ISO-8601 UTC",
  "kind": "work|partial_work|preference|correction|decision|remember|forget|pattern|verified_fact|procedure|pitfall",
  "scope": "user|project",
  "evidence": "tool_result|partial_tool_result|user_explicit|repeated_observation",
  "signals": ["tech_level=expert"],
  "status": "（可选）turn 未完成时的状态说明",
  "reason": "（可选）未完成原因"
}
```

`kind` 的权威枚举以 `engine/memory/policy.py` 的三个 frozenset（`MANUAL_MEMORY_KINDS` / `DURABLE_MEMORY_KINDS` / compile 的 `_RECENT_KINDS`）为准。`partial_work` 记录未完成 turn 的工具进展（`evidence: partial_tool_result`，附 `status`/`reason`），不得晋升为完成事实。Nudge 候选事件另带 `content`/`evidence_type`/`origin` 字段。

事件字段超过 16K 字符时保留首尾并显式标记截断。密钥和已知提示词注入行在写入前删除。`recent.jsonl` 是编译证据源，不是直接给回答模型读取的记忆。

周期 Nudge 产生的候选使用普通的稳定 `kind`，并额外标记 `origin: "periodic_nudge"`。它的 `summary` 只保存已验证工具结果中的精确支持性摘录；没有这条摘录、含密钥/注入、或表示当前任务状态的候选都会被拒绝。

## 5. 三个正式记忆视图

三份 Markdown 的标题、章节、准入规则、字符预算和冲突处理全部定义在 `MEMORY_POLICY.md`。输出文件只保存结果，不复制规则。

| 视图 | 输入筛选 | 更新方式 | 上限 |
|---|---|---|---|
| `context.md` | `scope=user` 的明确信号与稳定模式 | 用完整用户证据更新当前文件 | 4K 字符 |
| `recent.md` | 近期 work/decision/correction/remember/forget | 完整重建 3 天窗口；无内容时回退 7 天；仍为空则清除 | 8K 字符 |
| `durable.md` | durable offset 后的项目事实、决定、纠正、记住、忘记和受限周期候选 | 增量合并到当前文件 | 10K 字符 |

用户偏好只进入 `context.md`；近期状态只进入 `recent.md`；稳定项目共识只进入 `durable.md`。同一事实更新原条目，不在文件末尾无限追加。

### 5.1 Compiler

Compiler 每次只处理一个目标视图，并接收：

- 该视图对应的 MemoryPolicy；
- 当前已接受 Markdown；
- 筛选、清洗后的证据；
- 完整输出要求。

Compiler 必须返回完整 Markdown 文档。输入指纹不变时跳过重复编译。

### 5.2 Reviewer

Reviewer 同时读取：

- 目标视图的审核规则；
- 当前已接受 Markdown；
- 本次证据；
- Compiler 草稿。

它沿用最多三轮的生成—审核—反馈重试机制，并返回结构化结果：

```json
{"pass": true, "hard_fail": [], "soft_fail": [], "feedback": ""}
```

最终仍未通过、Reviewer 缺失或审核超时，都视为本次编译失败。

### 5.3 确定性写入

Reviewer 通过后，代码继续检查：

- 路径没有逃出 Agent profile；
- 一级标题和二级章节与 Policy 完全一致；
- 没有代码围栏且未超过字符预算；
- 没有密钥或提示词注入内容。

检查通过后，若旧文件非空且内容有变化，先保留旧文件 `.bak`（首次写入与内容未变时不产生备份），再使用临时文件和 `os.replace()` 原子替换。所有写入路径（通过、fallback、初始化、清除）都收敛到同一个确定性校验函数 `_commit_view()`。成功、未变化、fallback、拒绝和异常都会追加到 `memory_history.jsonl`，日志只保存哈希、轮次和脱敏错误，不复制记忆正文。

## 6. Compile 调度与 offset

`run_compilation()` 的顺序是：

```text
compile_context → compile_recent → compile_durable → sync_durable_topics（可选第四阶段）
```

前三个视图各自失败、各自记录审计，不会让未审核内容进入其他视图。第四阶段 topic sync 在 `sync_topics=True` 且 durable 编译成功（或存在 `sync_pending` 待办）时执行；生产维护路径固定开启。它消费**已审核**的 durable bullets，由 classifier 分组为至多 8 个 topic，逐 topic 复用 `compact_episode` 的 Generator/Reviewer 链路生成 `memory/episodes/` 下的 topic 页面，并把条目→页面的路由写入 `topic-associations.json`。topic sync 失败时写 `sync_pending`、记 `.topic_sync_retry_attempt` 冷却，并追加 `target="topic_sync"` 的审计记录。

- `.fp_context`、`.fp_recent`、`.fp_durable`：输入指纹；
- `.compile_offset`：本轮完整编译进度；
- `.durable_offset`：长期记忆已经消费到的事件行；
- `.dream_offset`：当前 `recent.jsonl` 中已经被成功 Dream 对账的事件行；只在完整证据批次通过后前进，日志截断后按剩余行数同步回退；缺失时从零开始，链接、损坏或负值状态会失败关闭并保留计数重试；
- `.dream_commit.json`：durable 已审核替换、但对应 `.dream_offset` 尚未确认时的恢复日志；只保存旧/新哈希和行 offset，不保存记忆正文；
- `.dream_cleanup.json`：`recent.jsonl` 已审计前缀的回收日志，保存旧/新日志哈希和 compile、durable、Dream 的目标 offset；启动时优先完成它，避免日志已截断而 offset 尚未同步时重放证据；
- `.compile_counter`：普通事件编译计数器。
- `.dream_counter`：Dream 的有效回合计数器。
- `.nudge_counter`：有效记忆回合的质量检查计数器，达到 20 时触发；成功得到空候选或已记录候选才归零。
- `.nudge_offset`：已经由 Nudge 审阅过的 `recent.jsonl` 行；拒绝、超时或异常时不前进。
- `.compile_pending` / `.nudge_pending` / `.dream_pending`：共享 LLM 客户端下延迟执行的待处理标记；维护状态将其作为对应 lane 的 `pending` 报告。
- `.{compile,nudge,dream,topic_sync}_retry_attempt`：重试冷却标记（见下）。
- `.maintenance`：维护任务的进程间锁文件。

`recent.md` 始终基于完整滚动窗口重建，不从 compile offset 截断；`durable.md` 始终从 durable offset 增量读取。只有成功消费的层才更新自己的指纹或 offset，失败后下次继续重试。

**重试与退避**：失败按类型区分处理——审核拒绝、内容不合规等"内容性失败"立即可重试；传输或 provider 失败（如 401、超时）则写入对应 lane 的 `.{lane}_retry_attempt` 标记，600 秒内跳过该 lane 的计数器触发，避免持续故障时每轮都烧一次 LLM 调用。显式学习信号触发的编译不受冷却约束；计数器触发受冷却约束。

### 6.1 周期 Nudge：20 回合的候选质量门

Nudge 的作用是把重复出现、且有工具结果支撑的工作经验送回已有质量链路，而不是让普通工作自动变成长期事实：

```text
recent.jsonl[.nudge_offset:] 中的 completed work/tool_result
  → Generator 提出至多两个 project 候选（也可为空）
  → Reviewer + 精确证据/安全/瞬时任务状态校验
  → rejected/failed：审计，counter=20，offset 不动，下一次或 idle tick 重试
  → unchanged：审计，推进 offset，counter=0
  → written：追加 origin=periodic_nudge 的 JSONL 候选
      → 既有 Compiler → Reviewer → durable.md 原子写入
```

候选只允许 `decision`、`verified_fact`、`procedure` 或 `pitfall`，且必须引用输入摘要中的逐字证据。它不能使用 `memory_ops`，不能直接创建 Markdown，也不能把 Todo、计划、当前状态或下一步写入候选。候选事件会先于 `.nudge_offset` 落盘；若在两者之间中断，重试会对完全相同的候选去重，而不会静默跨过证据。若 Compiler 随后失败，候选和 `compile` pending 标记会保留，由既有重试机制处理；Nudge 本身已经完成，不会丢失该批证据。

## 7. Dream

Dream 继续沿用每 50 个有效 turn 的低频机制，但它是长期记忆的**对账闭环**，而不只是文案压缩：

```text
当前 durable.md
  + recent.jsonl[.dream_offset:]
  → 清洗所有字段并切成完整 durable 证据批次
  → 每批 Generator 生成校正稿 → Reviewer 审核与确定性校验
  → 每个审核通过结果先写 .dream_commit.json（包括文本未变化）
  → 备份并原子替换 durable.md（若有变化）
  → 确认 durable 哈希后推进该批 .dream_offset
  → 所有批次完成后，先写 .dream_cleanup.json，再替换 JSONL 并按日志同步三个 offset
```

1. 对 `context.md`、`recent.md`、`durable.md` 和 episodes 做确定性密钥与注入清洗；
2. 将当前 durable 作为已接受基线，把两次成功 Dream 之间的 JSONL 增量作为唯一的事实性变更证据；普通 `work` 事件不能通过 Dream 晋升为长期记忆；所有渲染字段（含 `evidence` 等元数据）都先经过安全清洗；
3. 增量超过输入预算时按完整事件批次依次处理。单个合法事件超过输入预算时会生成带明确省略标记的有界安全投影，而不是失败或无限重试；一个 checkpoint 只覆盖已经送入 Generator 和 Reviewer 的事件投影，未见行不会被跳过；
4. Generator 只能保留、补充、修正或删除有证据支持的条目；未被增量矛盾的旧条目必须保留；
5. Reviewer、结构、预算、路径和安全校验全部通过后，先写入恢复日志（即使 durable 文本未变化）；确认目标 durable 哈希后才推进该批 checkpoint。若 checkpoint 写入中断，下次仅根据恢复日志完成 checkpoint，不重新调用模型；
6. 清理上限是 `min(compile_offset, durable_offset, 本次 Dream 已审计 offset)`，且只清理连续的过期前缀。清理前写入 `.dream_cleanup.json`，其中含旧/新日志哈希与三个目标 offset；若中断，下次先恢复 cleanup journal，再读取任何新证据，避免丢失、跳过或重放。

`MemoryMaintenanceService` 对 Dream 使用有界维护超时；超时、证据不可读、恢复/清理失败都会保留计数供重试，并写入脱敏 `memory_history.jsonl`。`memory_history.jsonl` 仅记录哈希、状态和错误，不能作为 Dream 的事实证据。

## 8. 召回与 Prompt 组装

单次推理采用分层加载：

```text
常驻：context.md
被动工作记忆：recent.md
按需第一层：durable.md 中与当前问题关键词匹配的条目
按需第二层：被命中 durable 条目路由到的 topic 页面（FTS + 可选向量混合召回）
```

`preparation.prepare_runtime()` 明确调用 `assemble_memory(include_durable=False)`，因此生产路径下 durable 不会整份常驻。主检索入口是 `retrieve_relevant_memory()`，返回保留来源边界的 `RelevantMemory(durable=..., episodes=...)`——durable 与 episodes 在 prompt 中是两个独立层（Durable Memory Retrieval / Relevant Episodes），不是一段文本（`search_relevant_memories()` 只是把两段拍平的向后兼容包装）。召回链路是：durable 关键词召回 → 用 `topics_for_entries()` 反查命中条目被路由的 topic → 只有这些 topic 的页面进入 FTS 作用域；若配置了 embedding provider，还会额外做 `TopicVectorIndex` 余弦检索并与词法证据合并去重。存在语义命中时，注入段标题由 `## Relevant Episodes` 变为 `## Relevant Topic Knowledge`。任一检索失败都降级为空，不阻塞回答。

语义召回默认关闭：需在 profile `config.yaml` 的 `knowledge.embeddings` 段显式启用（`enabled`/`base_url`/`model`/`api_key_env`），key 通过环境变量提供。provider 缺失、超时（10 秒）或异常只影响 topic 页面召回质量，静默降级为纯 FTS，不阻塞回答。

生产路径由调用方把记忆文本传入 `PromptAssembler`；assembler 另保留一条自读 profile 文件的兼容回退路径（`memory_text` 为 `None` 时自读 `recent.md`/`durable.md` 整份装入 legacy 层），仅供未迁移的直接调用方使用。学习得到的 context 与项目记忆都会先清洗并加安全围栏，明确历史内容只是参考，不能覆盖系统指令、`SMITH.md`、当前用户请求或工具权限。预算不足时可裁剪 recent/检索记忆，但常驻 `context.md` 不被裁剪；其自身由 4K Policy 上限约束。

## 9. 模块边界

| 模块 | 只负责什么 |
|---|---|
| `memory/store.py` | 证据写入、compile/Nudge/Dream 计数器调度、重试冷却、durable/topic/episode 召回 |
| `memory/policy.py` | 加载唯一 Policy、解析视图配置、校验 Markdown、kind 枚举权威定义 |
| `memory/compile.py` | 视图筛选、Compiler/Reviewer 调用、fallback 文档、指纹与 offset、topic sync 编排 |
| `memory/knowledge.py` | durable 条目 → topic 页面的路由关联存储与 `sync_durable_topics` |
| `memory/search.py` | episodes/topic 页面的 FTS5 索引（trigram/CJK 切词、短词 LIKE 回退） |
| `memory/vector.py` | 可选的 topic 页面向量索引（`TopicVectorIndex`） |
| `memory/embeddings.py` | OpenAI 兼容 embedding provider 及其配置解析 |
| `memory/_files.py` | 唯一的确定性写入与清洗原语（原子写、进程锁、密钥/注入清洗），被所有层依赖 |
| `memory/nudge.py` | 每 20 个有效记忆回合的受限候选发现、精确证据和安全校验 |
| `memory/_review.py` | 通用生成—审核重试协议 |
| `memory/history.py` | 追加脱敏审计记录（含保留窗口裁剪与失败连击统计） |
| `memory/dream.py` | 低频清洗、durable 与事件增量对账、checkpoint 和受限日志回收 |
| `memory/user_learner.py` | 只产出稳定偏好信号，不写 Markdown |
| `memory/maintenance.py` | 生命周期锁、超时、失败分类退避，以及由执行层注入的 LLM 依赖 |
| `engine/context/assembler.py` | 组合已接受的记忆，不理解编译规则 |

新增规则或模板优先修改 `MEMORY_POLICY.md`；外部调用方不需要了解具体模型提示词和文件写入细节。三份正式 Markdown 的全部写入路径（通过 / fallback / 初始化 / 清除）都收敛到 `_commit_view()` 这一个确定性校验函数。

## 10. 失败语义

| 失败 | 结果 |
|---|---|
| Compiler 异常或超时（`context.md`，或 Reviewer 缺失时） | 不写文件，保留旧视图，记录 `failed` |
| Compiler 超时 / 多轮审核未过（`recent.md`、`durable.md`，Reviewer 存在时） | 写入抽取式 fallback 文档，记录 `fallback`，推进指纹与 offset |
| Reviewer 拒绝或缺失 | 不写文件，保留旧视图，记录 `rejected` |
| Markdown 结构/预算不合规 | 不写文件，记录 `rejected` |
| 原子写入失败 | 临时文件清理，旧文件保持，记录 `failed` |
| Nudge 候选为空 | 记录 `nudge=unchanged`，推进 Nudge checkpoint，不写正式记忆 |
| Nudge 候选不合规、Reviewer 拒绝或提供方失败 | 不追加候选，不写正式记忆，保留 `.nudge_counter=20` 与 offset 供重试 |
| durable/episode 检索失败 | 本轮不注入对应记忆，回答继续 |
| topic sync 失败 | 写 `sync_pending` 与冷却标记，下次 durable 编译后重试，记录 `target="topic_sync"` 审计 |
| 记忆收尾失败 | 当前对话结果不回滚，后续维护重试（传输/provider 失败有 600 秒冷却） |

总原则：记忆可以暂时变旧，但不能用未经审核或损坏的内容替换已接受记忆，也不能阻塞用户当前任务。fallback 写入不违反此原则：它不含模型新增内容，只做确定性证据抽取，且仍通过全部结构与安全校验。

**可观测性**：`maintenance_status()`（经 `GET /api/agent/memory/status` 暴露）报告 compile/nudge/dream/topic_sync 四个 lane 的 idle/pending/running 状态，以及 `consecutive_failures`（从 history 尾部统计的连续失败数）与 `last_error`。topic_sync 在 compile lane 内执行，因此永不显示为 `running`。

## 11. 验收标准

1. 无工具调用时，明确偏好、纠正、决定、记住或忘记仍会写入证据。
2. 普通无价值纯聊天不会写入记忆。
3. 稳定偏好达到三次后进入 evidence，再由 Compiler/Reviewer 更新 `context.md`。
4. 三个 Markdown 都严格符合同一份 MemoryPolicy。
5. Reviewer 拒绝或缺失时，旧文件和指纹不变，并产生审计记录；`recent.md`/`durable.md` 在 Compiler 超时或多轮审核未过时可写入抽取式 fallback（审计 status 为 `fallback`）。
6. `recent.md` 在 3–7 天窗口为空时被清除。
7. durable offset 防止旧事件重复合并。
8. Dream 只根据当前 durable 与 `.dream_offset` 后的 JSONL 证据校正；审核失败时 durable、checkpoint 和未审计日志不变。
9. Dream 的单个超大合法事件会以显式有界投影被处理，不会永久阻塞 checkpoint；审核通过但文本未变、durable 替换和 JSONL cleanup 的中断都不得触发同一证据的模型重放。
10. 正常回答不读取 `recent.jsonl` 或 `memory_history.jsonl`。
11. 生产 `prepare_runtime` 路径下 durable 不整份常驻，只按当前问题召回匹配条目（assembler 的自读兼容回退路径除外）。
12. `SMITH.md` 永远不被自动学习修改。
13. engine、server 回归测试与 wheel package-data 校验通过。
14. 20 个有效记忆回合后，Nudge 的空候选不会生成 durable 内容；合格候选必须先以 `origin=periodic_nudge` 进入 JSONL，再经既有 Compiler/Reviewer 链路；拒绝或失败时计数保持待重试。

## 12. 当前限制

- durable 主体召回仍是有界关键词匹配；被 durable 路由到的 topic 页面在启用 embeddings 时走向量 + FTS 混合召回，未启用时降级为纯 FTS。同义改写在 durable 主体层仍可能漏召回。
- 向量索引是 `vectors.json` 全量扫描（余弦相似度），只覆盖 topic 页面；embedding 请求超时 10 秒后静默降级。适合当前小规模数据，规模增长后再评估。
- 事件分类使用小型确定性信号集；复杂隐含偏好只有在重复启发式命中或用户明确表达后才进入正式记忆。
- Nudge 的“是否足够可复用”仍需要 Generator/Reviewer 的语义判断；精确摘录、稳定 kind、确定性安全检查和可重试审计用于限制误判，而不是把 20 回合周期本身当作事实证据。
- `.bak` 只保存上一个版本（且仅在内容有变化时产生）；变更轨迹依赖 `memory_history.jsonl` 的哈希和原始证据日志，但该审计日志是**有界的**——Dream 定期裁剪到 500 条 / 90 天。

当前实现基线：2026-08-05（durable-routed topic knowledge 合入后）。规则以 `engine/memory/MEMORY_POLICY.md` 为准，行为以 `engine/tests/memory/test_memory_policy.py`、`test_memory_pipeline.py`、`test_memory_maintenance.py`、`test_topic_knowledge.py` 与 `test_memory_files.py` 为准。
