# Agent-Smith Agent Loop — 运行机制说明

> 本文面向需要理解或改造 Agent-Smith 执行内核的开发者，说明一次用户输入如何变成一条事件流：谁做路由、谁跑循环、循环在什么条件下停、停下来时留下了什么。内容以 `engine/` 代码为准，不以旧版设计稿为准。

---

## 一、一句话结论

Agent-Smith 的 Agent Loop 不是"一个 while 循环调 LLM"，而是**一条带预算、带终态、带补偿语义的事件流水线**：

```
一次用户输入 ──▶ 一个 run ──▶ 一串 ExecutionEvent ──▶ 一个确定的终态
```

所有对外可见的行为（流式文本、工具调用、门禁判定、审批弹窗、上下文压缩）都是这条流水线上的事件，没有旁路。

---

## 二、三条执行路径

`engine/execution/orchestration/agent_loop.py::run_agent_stream` 是唯一的分派点。它只做一件事：决定这次请求走哪条路径，然后把事件原样透传出去。

| 路径 | 触发条件 | 实现 | 特点 |
|---|---|---|---|
| **强制技能** | 请求显式指定 `forced_skill` | `_run_forced_skill_stream` | 单技能一次性执行，不过门禁 |
| **直接 ReAct** | `route.pipeline_id is None` | `react_event_loop` | 默认路径，绝大多数请求走这里 |
| **管线** | 路由命中声明式 pipeline | `run_pipeline` | 多节点串行，每节点带门禁 |

一个例外值得记住：`grill-me` 虽然形式上是 forced skill，但会被识别为 `requirements-research` 管线的入口而不是一次性技能调用（`grill_me_chain_entry`）。上游 skill 的语义是"一次访谈"，而在 Agent-Smith 里它是整条需求链的第一站。

**路由是纯词法的。** `IdentityCatalog` 按关键词 / 示例 / 优先级匹配，没有 LLM 兜底分类器。历史上存在过一个：它在每次关键词未命中时都要跑一遍，拖慢了普通 ReAct 回合，还可能启动用户根本没要求的多步工作流。现在的规则是——**管线必须由明确声明的高置信意图触发**，路由无法凭空发明身份、领域或管线。

已声明的路由只有四条：

| 身份 | 路由 | 管线 | 优先级 |
|---|---|---|---|
| `coding` | `requirements-research` | requirements-research | 30 |
| `coding` | `tdd-development` | tdd-development | 20 |
| `coding` | `code-review` | code-review | 10 |
| `smith` | `git` | 无（仍走 ReAct） | 30 |

普通编码、改 bug、重构请求**故意不被关键词劫持**，一律留在直接 ReAct。

---

## 三、ReAct 单轮的完整形状

`engine/execution/react/react_loop.py::react_event_loop`（1386 行，全系统最大的单文件）。一轮的顺序是固定的：

```
① 硬上限裁剪    conversation > 40 条 → 保头 2 + 保尾 28，切点必须落在配对边界
        ↓
② 预算测量      measure_request → 是否已越过压缩触发线
        ↓
③ 请求装配      fit_request：剪工具输出 → 模型压缩 → 确定性裁剪
        ↓         不 fit → INCOMPLETE(context_capacity_exhausted)，直接结束
④ 调用模型      优先 chat_events 流式；失败按条件回退 chat
        ↓
⑤ finish 分诊   length / content_filter / error / other / stop
        ↓
⑥ 无工具调用    → 收敛成最终答复（可能先触发续写或"假结尾"修复）
        ↓
⑦ 有工具调用    逐个走：可见性 → ToolPolicy → 审批 → PreHook → 执行 → PostHook
        ↓
⑧ 记账          round_had_success / failure / preflight → 决定消耗哪个预算
```

### 关键设计：切点不能拆散配对

第 ① 步的裁剪不是简单切片。`assistant(tool_calls)` 和它对应的 `tool` 结果必须成对出现，否则**下一次**请求会被 provider 整个拒收（OpenAI 400 / Anthropic `tool_use ids were found without tool_result blocks`）。所以切点要往前回退到 `assistant`/`user` 边界；如果回退撞到保留头部，就改为**往后**找边界——丢得更多，但配对完整且一定有进展。

### 关键设计：三个独立预算

| 预算 | 常量 | 值 | 递增条件 |
|---|---|---|---|
| 生产性迭代 | `DEFAULT_MAX_REACT_ITERS` | 60 | 本轮至少一个工具成功 |
| 失败恢复 | `MAX_FAILED_TOOL_RECOVERY_ITERS` | 20 | 本轮只有失败、没有成功 |
| 事实门禁挑战 | `MAX_PREFLIGHT_CHALLENGE_ITERS` | 20 | 本轮只被 preflight 挑战 |
| 同一错误重复 | `MAX_IDENTICAL_TOOL_ERRORS` | 6 | 同名工具 + 同前缀错误连续出现 |
| 截断续写 | `MAX_LENGTH_CONTINUATIONS` | 2 | `finish_reason == "length"` |
| 假结尾修复 | `MAX_INCOMPLETE_FINAL_REPAIRS` | 2 | 结尾只是"接下来我去查…" |

纯 preflight 轮**不消耗任何主预算**——它没有产生证据，也没有失败，只是被要求先去看一眼。把"在干活"和"在打转"用可判定信号分开，是这套预算能同时防死循环又不误杀长任务的原因。

### 关键设计："假结尾"检测

`looks_like_incomplete_final_after_tool` 用中英双语正则匹配"让我 / 我将 / 接下来 + 查 / 搜 / 验证"这类句式（240 字以内）。模型在成功调用过工具之后，如果最终答复只是一句承诺继续行动，那不是答复。这时把它当成未完成，追加一次修复提示重跑，最多两次。

---

## 四、终态与原因

一次 run 只能以三种事件之一收尾，reason 字段是可穷举的：

| 终态 | reason | 含义 |
|---|---|---|
| `DONE` | — | 正常结束（可能前面还带过 INCOMPLETE） |
| `INCOMPLETE` | `context_capacity_exhausted` | 装配后仍超预算 |
| | `context_limit` | provider 拒收长度，且已恢复过一次 |
| | `model_output_limit` | 输出被截断，续写次数用尽 |
| | `content_filter` | provider 内容过滤 |
| | `unknown_provider_finish_reason` | 无法解释的 finish_reason |
| | `empty_model_response` | 无工具调用也无文本 |
| | `identical_tool_error_loop` | 同一错误重复 6 次 |
| | `tool_failure_budget` | 失败恢复预算耗尽 |
| | `preflight_budget` | 事实门禁挑战预算耗尽 |
| | `tool_call_budget` | 生产性迭代预算耗尽 |
| `FAILED` | `provider_finish_error` | provider 报告本次生成失败 |

> `empty_model_response` 是一条容易被忽略的重要判定：**一次成功的工具调用不是一次合法的对话完成**。调用方仍然需要一段可渲染、可落库的最终答复；只有工具结果没有答复，算未完成。

---

## 五、事件契约

`engine/execution/events.py` 定义 27 种 `EventType`。按用途分四组：

| 组 | 事件 |
|---|---|
| **生命周期** | `RUN_STARTED` `ROUTE_DECIDED` `DONE` `INCOMPLETE` `FAILED` `RUN_FINISHED` |
| **内容** | `RAW_RESPONSE_EVENT` `THINKING` `TEXT_DELTA` `SMITH_UI` `SMITH_UI_FALLBACK` |
| **工具与门禁** | `TOOL_CALL_START` `TOOL_CALL_RESULT` `SKILL_START` `SKILL_END` `GATE_RESULT` `GATE_EVIDENCE` `BACKTRACK` `BLOCKED` `AWAITING_INPUT` |
| **计量与草稿** | `TOKEN_USAGE` `CONTEXT_USAGE` `CONTEXT_COMPRESSION_START` `CONTEXT_COMPRESSION_END` `PROVISIONAL_TEXT_DELTA` `PROVISIONAL_COMMIT` `PROVISIONAL_RETRACT` |

### 草稿三件套（provisional lifecycle）

流式输出有个固有矛盾：文本已经打到用户屏幕上了，但这一轮**还没决定它算不算答复**。模型可能接着发起工具调用（那这段就是前言，不是答复），也可能撞上内容过滤、上下文超限、门禁驳回。

Agent-Smith 的处理方式是给每段流式草稿分配 `provision_id`，然后显式结算：

```
PROVISIONAL_TEXT_DELTA(id, text)   草稿正在流出
        ↓
   ┌────┴────┐
COMMIT(id)   RETRACT(id, reason)
 认账          撤回，消费方删掉已渲染的文本
```

撤回原因是有分类的：`tool_call_pending`（转去调工具）、`content_filter`、`context_limit`、`rubric_retry`（门禁驳回）、`stream_error`、`tool_call_budget`、`execution_error`。

配套的坑也在代码注释里写明了：草稿被 retract 之后，如果再发一个带 `already_streamed` 标记的 `TEXT_DELTA`，消费方会跳过渲染 → **文本落库但用户永远看不到**。所以 retract 之后的补发一律不带该标记。

---

## 六、Run 状态机与崩溃恢复

`engine/execution/orchestration/run_state.py` 定义 7 个状态：

```
QUEUED ──▶ RUNNING ──┬──▶ COMPLETED
                     ├──▶ INCOMPLETE
                     ├──▶ FAILED
                     ├──▶ CANCELLED
                     └──▶ WAITING_APPROVAL ──▶ RUNNING
```

事件通过 `_RunEventBoundary` 双写：一路投影进 `RunStateStore`，一路给可观测性 observer。这个边界有两个细节：

1. 持久化 I/O（带 fsync）被 `asyncio.to_thread` 挪到工作线程，磁盘忙不会卡住 server 的事件循环；
2. 每个 boundary 有一把锁做**串行化**——`to_thread` 本身不保证执行顺序，而 trace 的序号和字节游标依赖记录按流顺序落盘。

### Checkpoint 的两种语义

管线执行时会存 checkpoint。恢复时区分两种情况：

| 场景 | 判据 | 恢复行为 |
|---|---|---|
| 崩溃恢复 | 消息与 checkpoint 记录的原始请求**逐字相同** | 跳过已完成节点，从 `index + 1` 继续 |
| 有意暂停 | checkpoint 标了 `awaiting_user_input` | 停在**同一节点**，新消息作为 `user_response` 注入 |

这里有一个必须处理的竞态：**光靠请求内容无法区分"上一个 run 崩了"和"上一个 run 还在跑"**。客户端超时后重发、用户手抖点两次，都会让新 run 去接管一个活跃 run 的半成品状态，两个 run 随后无协调地写同一个工作目录。

解法是 `_checkpoint_owner_still_running`：查 owner run 的状态，`QUEUED`/`RUNNING`/`WAITING_APPROVAL` 都算活着；查不到就**失败关闭**（当成活着）。判定为活着时，新 run 从头开始，并且**保留对方的 checkpoint**——顺手清掉会删掉一个还在执行的 run 的崩溃恢复点。

---

## 七、管线执行

`engine/execution/pipeline/pipeline.py::run_pipeline`。节点串行走，每个节点是"跑一次 ReAct + 过两层门禁"。

```
节点 N
  ├─ condition(context) 为假 → 跳过（并清掉 retry_hint，避免泄漏给下一个节点）
  ├─ allowed_tools 声明 → tool_registry.scoped_to(...) 得到节点级能力视图
  ├─ 技能存在？ ── 是 ─▶ execute_skill_events（技能正文 + 节点契约追加段）
  │               └ 否 ─▶ execute_react_fallback_events（节点级 ReAct 兜底）
  ├─ 第一层：chain.base_gates（YAML 声明的兜底层，可为空）
  ├─ 第二层：node.gate（领域门禁，可能是 LLMGate）
  └─ 判定 ─▶ pass / retry / backtrack / blocked
```

### 门禁失败的四级处理

| 情形 | 动作 |
|---|---|
| 专用技能未通过门禁 | 把门禁反馈作为 `retry_hint`，**同一节点**改走 ReAct 重试 |
| ReAct 兜底也未通过 | 交给 `FailureLoopGuard`，返回 `retry` / `switch` / `blocked` |
| `switch` 且有 backtrack 目标 | 回溯，**淘汰目标节点及其之后所有已提交输出**，带上 retry_hint |
| `switch` 但无目标 / 回溯超 5 次 | 降级为 `blocked`，终止 |

回溯时驱逐旧输出是必须的：留着会让第一遍的过期结果泄漏进重跑技能的交接上下文，也会泄漏进最终答复（最终答复取最后一个已提交输出）。

同样地，回溯必须把失败原因带到目标节点。否则 planning 重跑时 messages 还是最初的原始需求，模型拿不到"为什么被打回"的任何信号，大概率复现同一份产出——而 `FailureLoopGuard` 按 skill 累计、不因回溯重置，等于整条流水线只有一次纠错机会，且这次机会因无信息传递而大概率无效。

### 一个刻意的设计：技能缺失不阻塞

管线节点找不到已安装的 `SKILL.md` 时，退化为节点级 ReAct 兜底，**门禁照旧运行**。这样中间产物的契约仍然可观测，不会因为少装一个技能就整条链失效。

---

## 八、Hook 三点接入

| 类型 | 时机 | 能否阻断 | 返回 |
|---|---|---|---|
| `PreToolHook` | 工具执行前 | ✅ 能 | `(allowed, denial_reason)` |
| `PostToolHook` | 工具执行后 | ❌ 不能 | `list[str]` 警告，注入为 system 消息 |
| `StopHook` | 每次响应结束 | ❌ 不能 | 无（通常异步） |

内置四个（`agents/smith/hooks/`）：`config-protection`（Pre，拦 linter/formatter 配置改动）、`console-warn`（Post）、`quality-gate`（Post，异步跑格式/lint）、`cost-tracker`（Stop，写 `~/.agent-smith/metrics/costs.jsonl`）。

> PreHook 阻断路径上有一条**必须**做的事：补一条配对的 `tool` 结果消息。每个 `assistant.tool_calls` 条目都必须有配对结果，否则下一次请求整个被 provider 拒收。这条分支曾是循环里唯一漏掉配对的阻断路径，而 `config-protection` 是默认开启的——编辑 `pyproject.toml` 就会走到这里。

事实门禁（首次编辑前要求先调查）**不是**可插拔 hook：它在 `engine/safety/fact_gate.py`，由 `lifecycle.py` 按请求装配，始终生效，只挑战不阻断。

---

## 九、上下文超限的两级恢复

这是 Agent Loop 里唯一带"重试同一轮"语义的机制。

```
provider 抛出上下文超限错误
        ↓
context_recoveries >= 1 ? ──是──▶ INCOMPLETE(context_limit)
        ↓ 否
撤回所有活跃草稿（不撤回的话新旧两个 id 都会在结束时被 commit，
                客户端会继续渲染已经不存在的文本）
        ↓
CONTEXT_COMPRESSION_START
trim_conversation_for_context_limit(conversation)
CONTEXT_COMPRESSION_END
        ↓
continue —— 重跑同一轮
```

流式回退到非流式有严格条件：**只有当本次响应没有发出任何语义 delta、且没有更早的续写草稿仍然可见时**才允许回退。否则回退可能重播用户没见过的后缀，或者产出一份不同的工具计划。判据是 `saw_content_event`——文本、推理、工具参数三种 delta 任一出现即为真（推理 delta 用户看不见，但同样证明 provider 已经开始生成了）。

---

## 十、可观测性落点

| 数据 | 位置 |
|---|---|
| 逐事件 trace | `{profile_dir}/traces/{run_id}.jsonl`（哈希链，可验篡改） |
| run 索引 | SQLite（`ObservabilityIndex`），带保留策略 |
| run 摘要 | `RunSummaryStore` |
| 提示词溯源 | `prompt_manifest`（16 层的 source / authority / trust / hash） |
| 工具审批审计 | `~/.agent-smith/audit.jsonl`（哈希链 + `.head` 锚点） |
| 成本 | `~/.agent-smith/metrics/costs.jsonl` |
| 异常 | `IncidentDetector` 从 trace 派生 |

trace 持久化是**刻意的 best-effort**：本地 trace 不可用绝不能把一次本来有效的 Agent run 变成失败执行。

---

## 十一、全链路总览

```
用户输入（Ink shell）
        ↓ HTTP + SSE
server/app/services/session_service.py
        ↓
engine/execution/orchestration/lifecycle.py     run 生命周期、审批上下文、事实门禁装配
        ↓
        preparation.py                          路由 → 工具 → 技能 → Hook → 记忆 → 16 层提示词
        ↓
        agent_loop.py                           三路分派 + checkpoint 恢复
        ↓
   ┌────┴─────────────────────┐
react_loop.py               pipeline.py
（预算 / 草稿 / 工具 / 恢复）  （节点 / 两层门禁 / 回溯）
   └────┬─────────────────────┘
        ↓
   ExecutionEvent 流
        ↓
   ┌────┴────┐
RunStateStore   RunEventObserver（trace / 索引 / 摘要 / 异常）
        ↓
        lifecycle 收尾：记忆落库、偏好学习、Stop Hook、终态写入
```

---

## 十二、常见问题

**Q: 为什么不用 LLM 做意图分类？**

试过，去掉了。它在每次关键词未命中时都要跑一次，拖慢了普通 ReAct 回合；更糟的是它会**启动用户没要求的多步工作流**。现在的立场是：管线是重决策，必须由明确声明的意图触发；不确定就留在 ReAct，代价最小。

**Q: `max_iters = 60` 会不会太少 / 太多？**

它只数**生产性**迭代——本轮至少有一个工具成功。失败重试、事实门禁挑战各有独立预算，不占这 60。一个正常的大任务很难连续 60 次成功调用工具还没得出结论；而一个在打转的任务会先撞上 `identical_tool_error_loop`（同一错误 6 次）或失败恢复预算（20 次），不会跑到 60。

**Q: 流式文本已经打到屏幕上了，还能撤回吗？**

能。这就是 `PROVISIONAL_RETRACT` 的用途——消费方收到后删除对应 `provision_id` 的已渲染文本。代价是消费方必须实现这个契约；只认 `TEXT_DELTA` 的简单客户端会看到被撤回的草稿留在屏幕上。

**Q: 管线节点的工具是怎么收窄的？**

节点 YAML 里声明 `allowed_tools`，`run_pipeline` 调 `tool_registry.scoped_to(...)` 得到一个节点级视图。如果运行时的 registry 不支持 `scoped_to`，直接抛错而不是静默放行——声明了能力边界却无法执行，是配置错误，不是可降级情形。

**Q: 一次 run 崩了，重发同样的消息会怎样？**

先查上一个 run 的状态。它还活着（`QUEUED`/`RUNNING`/`WAITING_APPROVAL`）或者状态查不到，就从头开始并保留它的 checkpoint；确认结束了才接管，跳过已完成节点。

**Q: 门禁反馈会传给模型吗？**

会，通过 `CTX_RETRY_HINT` → `CTX_RUBRIC_FEEDBACK`。第 3 次尝试（`attempt == 2`）不再传具体反馈，改为固定的"换一个完全不同的思路"。跳过的节点会主动清掉 `retry_hint`，避免上一个节点的反馈泄漏成下一个节点的评分依据。

---

**相关文档**：[上下文治理](22-上下文治理.md) · [记忆系统](21-记忆系统.md) · [工具与安全体系](23-工具与安全体系.md)
