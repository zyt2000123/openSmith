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
  → Compiler 按 MemoryPolicy 生成一个完整 Markdown 草稿
  → Reviewer 按同一份 MemoryPolicy、旧文件和证据审核
  → 通过：确定性代码校验、备份并原子替换
  → 拒绝/超时/异常：保留旧文件并记录审计结果
  → 后续对话固定加载 context、加载 recent、按问题召回 durable/episodes
  → 用户纠正、忘记请求和新任务结果再次进入证据流
```

Compiler 和 Reviewer 是两个模型角色。生产运行时分别使用 `RuntimeServices.llm` 与 `RuntimeServices.gate_llm`；正式 Markdown 没有 Reviewer 明确通过时不得写入。

写文件的代码只做确定性技术校验，不做第三次语义裁决。

## 3. 文件职责与路径

所有运行时记忆路径都相对当前 Agent profile：

| 文件 | 职责 | 正常回答是否读取 |
|---|---|---|
| `SMITH.md` | 用户手写规则与项目指令 | 固定加载，自动记忆永不写入 |
| `engine/memory/MEMORY_POLICY.md` | 三个正式视图唯一的生成、审核和格式规则 | 仅 Compiler、Reviewer、Dream 读取 |
| `context.md` | 已确认的用户偏好、协作模式和稳定用户背景 | 每轮固定加载 |
| `memory/recent.jsonl` | 追加式、已清洗的证据日志 | 不直接进入回答 Prompt |
| `memory/recent.md` | 最近 3–7 天仍需延续的工作 | 非空时加载 |
| `memory/durable.md` | 稳定项目事实、决定、流程和陷阱 | 仅查询命中时加载 |
| `memory/episodes/*.md` | 可检索的任务经历 | 仅查询命中时加载 |
| `memory/memory_history.jsonl` | 编译、审核和写入审计 | 不进入回答 Prompt |

`context.md` 位于 profile 根目录，是因为它属于 Smith 与用户之间的常驻协作上下文；项目记忆位于 `memory/`，便于独立维护、清理和检索。Policy 位于 Python 包内并作为 package data 发布，确保源码运行和 wheel 安装使用同一份规则。

## 4. 证据写入

### 4.1 何时记录

`save_conversation_memory()` 满足任一条件时才写证据：

1. 本轮实际调用了工具或技能；
2. 用户明确表达偏好、纠正、决定、记住或忘记；
3. `UserPreferenceLearner` 对同一模式累计观察达到 3 次。

普通闲聊、一次性问答和没有未来价值的纯聊天不会进入证据日志。

明确学习信号会立即触发一次编译；普通工具工作仍沿用每 5 个有效 turn 编译一次。写入失败时，学习器不会确认信号，下次观察会继续重试。

### 4.2 事件格式

旧的三字段事件继续兼容。新事件增加可选分类字段：

```json
{
  "task": "用户消息的已清洗文本",
  "summary": "Smith 回答的已清洗文本",
  "timestamp": "ISO-8601 UTC",
  "kind": "work|preference|correction|decision|remember|forget|pattern",
  "scope": "user|project",
  "evidence": "tool_result|user_explicit|repeated_observation",
  "signals": ["tech_level=expert"]
}
```

事件字段超过 16K 字符时保留首尾并显式标记截断。密钥和已知提示词注入行在写入前删除。`recent.jsonl` 是编译证据源，不是直接给回答模型读取的记忆。

## 5. 三个正式记忆视图

三份 Markdown 的标题、章节、准入规则、字符预算和冲突处理全部定义在 `MEMORY_POLICY.md`。输出文件只保存结果，不复制规则。

| 视图 | 输入筛选 | 更新方式 | 上限 |
|---|---|---|---|
| `context.md` | `scope=user` 的明确信号与稳定模式 | 用完整用户证据更新当前文件 | 4K 字符 |
| `recent.md` | 近期 work/decision/correction/remember/forget | 完整重建 3 天窗口；无内容时回退 7 天；仍为空则清除 | 8K 字符 |
| `durable.md` | durable offset 后的项目事实、决定、纠正、记住和忘记证据 | 增量合并到当前文件 | 10K 字符 |

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

检查通过后先保留旧文件 `.bak`，再使用临时文件和 `os.replace()` 原子替换。成功、未变化、拒绝和异常都会追加到 `memory_history.jsonl`，日志只保存哈希、轮次和脱敏错误，不复制记忆正文。

## 6. Compile 调度与 offset

`run_compilation()` 的顺序是：

```text
compile_context → compile_recent → compile_durable
```

三个视图各自失败、各自记录审计，不会让未审核内容进入其他视图。

- `.fp_context`、`.fp_recent`、`.fp_durable`：输入指纹；
- `.compile_offset`：本轮完整编译进度；
- `.durable_offset`：长期记忆已经消费到的事件行；
- `.compile_counter`：普通事件编译计数器。

`recent.md` 始终基于完整滚动窗口重建，不从 compile offset 截断；`durable.md` 始终从 durable offset 增量读取。只有成功消费的层才更新自己的指纹或 offset，失败后下次继续重试。

## 7. Dream

Dream 继续沿用每 50 个有效 turn 的低频机制，只整理，不创造新知识：

1. 对 `context.md`、`recent.md`、`durable.md` 和 episodes 做确定性密钥与注入清洗；
2. 按 MemoryPolicy 合并 durable 重复项、删除已替代内容并压缩措辞；
3. 使用 Reviewer 审核整理结果；
4. 通过后备份并原子替换；失败则保留旧 durable；
5. 在 compile 与 durable offset 都已消费且事件超过窗口后清理旧 JSONL 行。

`.dream_counter` 只在 Dream 完整成功或属于正常无事可做时归零；审核失败会保留计数，等待下一次维护。

## 8. 召回与 Prompt 组装

单次推理采用分层加载：

```text
常驻：context.md
被动工作记忆：recent.md
按需：durable.md 中与当前问题匹配的条目
按需：FTS5 命中的 episodes
```

`agent_loop.prepare_runtime()` 明确调用 `assemble_memory(include_durable=False)`，因此 durable 不会整份常驻。`search_relevant_memories()` 对 durable 条目做有界关键词召回，并继续对 episodes 使用 FTS5；任一检索失败都降级为空，不阻塞回答。

`PromptAssembler` 只负责组合调用方给出的记忆文本。学习得到的 context 与项目记忆都会先清洗并加安全围栏，明确历史内容只是参考，不能覆盖系统指令、`SMITH.md`、当前用户请求或工具权限。预算不足时可裁剪 recent/检索记忆，但常驻 `context.md` 不被裁剪；其自身由 4K Policy 上限约束。

## 9. 模块边界

| 模块 | 只负责什么 |
|---|---|
| `memory/store.py` | 证据写入、计数器调度、durable/episode 召回 |
| `memory/policy.py` | 加载唯一 Policy、解析视图配置、校验 Markdown |
| `memory/compile.py` | 视图筛选、Compiler/Reviewer 调用、指纹与 offset |
| `memory/_review.py` | 通用生成—审核重试协议 |
| `memory/history.py` | 追加脱敏审计记录 |
| `memory/dream.py` | 低频清洗、整理和日志回收 |
| `memory/user_learner.py` | 只产出稳定偏好信号，不写 Markdown |
| `memory/maintenance.py` | 生命周期锁、超时，以及由执行层注入的 LLM 依赖 |
| `prompt/assembler.py` | 组合已接受的记忆，不理解编译规则 |

新增规则或模板优先修改 `MEMORY_POLICY.md`；外部调用方不需要了解具体模型提示词和文件写入细节。三份正式 Markdown 始终只有一个写入入口。

## 10. 失败语义

| 失败 | 结果 |
|---|---|
| Compiler 异常或超时 | 不写文件，保留旧视图，记录 `failed` |
| Reviewer 拒绝或缺失 | 不写文件，保留旧视图，记录 `rejected` |
| Markdown 结构/预算不合规 | 不写文件，记录 `rejected` |
| 原子写入失败 | 临时文件清理，旧文件保持，记录 `failed` |
| durable/episode 检索失败 | 本轮不注入对应记忆，回答继续 |
| 记忆收尾失败 | 当前对话结果不回滚，后续维护重试 |

总原则：记忆可以暂时变旧，但不能用未经审核或损坏的内容替换已接受记忆，也不能阻塞用户当前任务。

## 11. 验收标准

1. 无工具调用时，明确偏好、纠正、决定、记住或忘记仍会写入证据。
2. 普通无价值纯聊天不会写入记忆。
3. 稳定偏好达到三次后进入 evidence，再由 Compiler/Reviewer 更新 `context.md`。
4. 三个 Markdown 都严格符合同一份 MemoryPolicy。
5. Reviewer 拒绝、缺失或超时时，旧文件和指纹不变，并产生审计记录。
6. `recent.md` 在 3–7 天窗口为空时被清除。
7. durable offset 防止旧事件重复合并。
8. Dream 不增加现有 durable 无法支持的事实。
9. 正常回答不读取 `recent.jsonl` 或 `memory_history.jsonl`。
10. durable 不整份常驻，只按当前问题召回匹配条目。
11. `SMITH.md` 永远不被自动学习修改。
12. engine、server 回归测试与 wheel package-data 校验通过。

## 12. 当前限制

- durable 按需召回目前是有界关键词匹配，不是向量语义检索；同义改写可能漏召回。
- episodes 仍使用本地 FTS5，适合当前小规模数据；规模增长后再评估混合检索。
- 事件分类使用小型确定性信号集；复杂隐含偏好只有在重复启发式命中或用户明确表达后才进入正式记忆。
- `.bak` 只保存上一个版本；完整变更轨迹依赖 `memory_history.jsonl` 的哈希和原始证据日志。

当前实现基线：2026-07-13。规则以 `engine/memory/MEMORY_POLICY.md` 为准，行为以 `engine/tests/memory/test_memory_policy.py` 与 `engine/tests/memory/test_memory_pipeline.py` 为准。
