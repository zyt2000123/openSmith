# 04a · ReAct Loop 设计

> **源文件**：`engine/execution/react/react_loop.py` (1170 行)
>
> **定位**：ReAct Loop 是 Agent-Smith 执行引擎的最内层循环——LLM 在单个技能步骤内的"思考 → 调工具 → 观察 → 再思考"核心循环。所有 Agent 输出（文本/流式/事件）最终都经过这里。

---

## 目录

1. [在执行层中的位置](#一在执行层中的位置)
2. [三层 API 与消费模型](#二三层-api-与消费模型)
3. [核心状态机](#三核心状态机)
4. [事件协议](#四事件协议)
5. [流式传输与 Provisional 协议](#五流式传输与-provisional-协议)
6. [预算与终止条件](#六预算与终止条件)
7. [会话压缩](#七会话压缩)
8. [工具策略集成](#八工具策略集成)
9. [异常传播](#九异常传播)
10. [设计权衡与已知局限](#十设计权衡与已知局限)

---

## 一、在执行层中的位置

```
task_router          ← 规则引擎：决定走哪条技能链
  └─ skill_chain     ← DAG 编排：步骤顺序 / 门禁 / 回退
       └─ react_loop ← 本文：LLM 单步内的自由思考循环  ◀
            └─ tool_policy ← 每次工具调用前的安全拦截
```

react_loop **只关心一件事**：拿到一组 messages，让 LLM 反复"生成 → 执行工具 → 把结果追加到对话 → 再生成"，直到 LLM 给出最终文本回复或预算耗尽。

它不知道：
- 当前在哪个技能节点（skill_chain 的事）
- 门禁是否通过（pipeline 的事）
- 会话持久化（session_service 的事）
- HTTP / SSE 传输（server 层的事）

---

## 二、一个生成器与它的消费者

`react_loop.py` 对外只暴露一个函数：

```
react_event_loop()   ← 核心：AsyncGenerator[ExecutionEvent]，全部事件
  ↑ 消费
engine/execution/orchestration/agent_loop.py   ← 生产侧唯一消费者，逐事件转发
engine/tests/execution/react_text_adapters.py  ← 仅测试用的取文本适配器
```

### 2.1 `react_event_loop` — 核心生成器

```python
async def react_event_loop(
    llm: LLMClient,
    messages: list[dict],
    tool_registry: ToolRegistry,
    tool_guard: ToolGuard | None = None,
    max_iters: int = 60,
    *,
    fact_gate: FactGate | None = None,
    provisional_lifecycle: bool = True,
) -> AsyncGenerator[ExecutionEvent, None]
```

**唯一的真正实现**。所有状态管理、预算控制、错误恢复都在这里。产出 `ExecutionEvent` 流，上层自选消费方式。

### 2.2 生产侧只有一个消费者

`agent_loop` 把事件原样转发给 `lifecycle`，`lifecycle` **先 `record` 再 `yield`**
（`orchestration/lifecycle.py`），SSE 拿到的是完整事件流而不是纯文本流。文本从不
在 react 层被拼装。

本模块早先还放过两个适配器：`react_loop()`（收集全文返回 `str`）与
`react_stream_loop()`（只 yield `TEXT_DELTA` 文本）。文档给它们的存在理由是
"CLI 单次对话"和"SSE 端点的直接文本流"——**两条路径都不存在**：CLI 走
shell → HTTP，SSE 走 lifecycle 事件流。九个测试调用它们，让它们看上去是活的；
但被测的一直是 `react_event_loop` 的行为，适配器只是测试取数的方式。现已移到
`engine/tests/execution/react_text_adapters.py`。

### 2.3 为什么核心只产事件

| 设计选择 | 理由 |
|---|---|
| 核心只产事件 | pipeline / skill_chain 需要完整事件流做门禁判断 |
| 不在本层拼文本 | 观测的记录边界在 `lifecycle`（先 record 后 yield）。任何绕过 lifecycle 的文本通道都会直接变成观测盲区 |

生成器共享**零状态**——所有状态都在 `react_event_loop` 的局部变量里。没有类、没有实例、没有 mutable 共享。

---

## 三、核心状态机

`react_event_loop` 用一组局部计数器而非显式状态枚举来驱动循环。以下是完整状态空间：

### 3.1 计数器

| 变量 | 语义 | 上限常量 |
|---|---|---|
| `productive_iters` | 含成功工具调用的轮次数 | `DEFAULT_MAX_REACT_ITERS` = 60 |
| `recovery_iters` | 纯失败轮（无任何成功调用）的累计 | `MAX_FAILED_TOOL_RECOVERY_ITERS` = 20 |
| `preflight_iters` | 被 FactGate 挑战的轮次累计 | `MAX_PREFLIGHT_CHALLENGE_ITERS` = 20 |
| `consecutive_errors` | 连续工具失败数（成功时归零） | 3 → 注入 TOOL_FAILURE_HINT |
| `identical_error_count` | 相同工具+相同错误的连续出现次数 | `MAX_IDENTICAL_TOOL_ERRORS` = 6 |
| `incomplete_final_repairs` | "假完成"修复次数 | `MAX_INCOMPLETE_FINAL_REPAIRS` = 2 |
| `length_continuations` | finish_reason=length 时的续写次数 | `MAX_LENGTH_CONTINUATIONS` = 2 |

### 3.2 主循环伪代码

```
while productive_iters < 60:
    compress(conversation)
    if conversation too long: hard-prune (keep head + tail)

    yield THINKING

    response = stream_or_fallback_chat(conversation, tools)
    yield TOKEN_USAGE

    if response has tool_calls:
        retract all provisional drafts  ← 重新进入证据收集
        handle finish_reason edge cases (length/error/content_filter)

        for each tool_call:
            policy.evaluate → blocked / challenged / allowed
            if allowed: execute, record result
            track errors, successes, preflight challenges

        update counters, check budgets
    else:  # 纯文本回复 → 可能是最终答案
        handle finish_reason:
            length     → 续写或 INCOMPLETE
            error      → FAILED
            filter     → INCOMPLETE
            other      → INCOMPLETE

        if looks_like_incomplete_final → repair prompt
        if has_text → commit provisional, yield TEXT_DELTA, return
        if empty    → INCOMPLETE(empty_model_response)

# 循环耗尽
yield INCOMPLETE(tool_call_budget)
```

### 3.3 终止出口总表

| 出口 | 事件 | 条件 |
|---|---|---|
| **正常完成** | `TEXT_DELTA` → 函数返回 | LLM 产出非空文本且非"假完成" |
| 空回复 | `INCOMPLETE(empty_model_response)` | LLM 给出空文本（通常因工具后遗忘总结） |
| 模型输出超限（文本） | `INCOMPLETE(model_output_limit)` | finish_reason=length 且续写次数已耗尽 |
| 模型输出超限（工具调用） | `INCOMPLETE(model_output_limit)` | finish_reason=length 时工具调用 JSON 被截断 |
| 内容过滤 | `INCOMPLETE(content_filter)` | finish_reason=content_filter |
| Provider 错误 | `FAILED(provider_finish_error)` | finish_reason=error |
| 未知终止原因 | `INCOMPLETE(unknown_provider_finish_reason)` | finish_reason=other |
| 工具调用预算 | `INCOMPLETE(tool_call_budget)` | productive_iters ≥ 60 |
| 工具失败预算 | `INCOMPLETE(tool_failure_budget)` | recovery_iters ≥ 20 |
| Preflight 预算 | `INCOMPLETE(preflight_budget)` | preflight_iters ≥ 20 |
| 相同错误循环 | `INCOMPLETE(identical_tool_error_loop)` | 同一工具+同一错误连续 ≥ 6 次 |

---

## 四、事件协议

react_loop 产出的所有事件类型：

| EventType | 数据 | 语义 |
|---|---|---|
| `THINKING` | `{}` 或 `{text, done: true}` | 一轮决策开始 / LLM 推理内容 |
| `RAW_RESPONSE_EVENT` | `{type, data, provision_id?}` | Provider 原始流事件透传 |
| `PROVISIONAL_TEXT_DELTA` | `{provision_id, text}` | 流式草稿文本（可能被撤回） |
| `PROVISIONAL_COMMIT` | `{provision_id}` | 草稿转正 → 变为持久文本 |
| `PROVISIONAL_RETRACT` | `{provision_id, reason}` | 草稿撤回（工具调用 / 错误 / 门禁失败） |
| `TEXT_DELTA` | `{text, already_streamed?}` | 最终文本增量 |
| `TOKEN_USAGE` | `{input_tokens, output_tokens, total_tokens}` | 每轮 token 用量 |
| `CONTEXT_USAGE` | `{context_tokens, safe_input_budget, fit_status, actions, ...}` | 每轮请求前后的上下文占用、判定结果，以及**本轮实际裁掉了什么**（`actions`：`pruned_tool_output_chars:N` / `compacted_history` / `deterministic_trim` / `compaction_failed` / `model_compaction_disabled`）。`fit_status` 区分不出"只剪了工具输出"和"整段历史被摘要替换"，`actions` 才能 |
| `CONTEXT_COMPRESSION_START` / `END` | `{reason}` / `{recovered, ...}` | 上下文压缩开始 / 结束（触发时） |
| `TOOL_CALL_START` | `{name, id, arguments}` | 工具执行开始 |
| `TOOL_CALL_RESULT` | `{id, error, blocked, preflight, ...}` | 工具执行结果 |
| `SMITH_UI` / `SMITH_UI_FALLBACK` | `{ui, ...}` / `{raw, ...}` | 结构化 UI 事件；无效 payload 降级为文本 |
| `INCOMPLETE` | `{reason, ...}` | 非正常终止（软失败） |
| `FAILED` | `{reason}` | 硬失败 |

### 事件顺序约束

```
THINKING → [RAW_RESPONSE_EVENT...] → TOKEN_USAGE
  ├─ 有工具调用:
  │    PROVISIONAL_RETRACT(all drafts)
  │    → [TOOL_CALL_START → TOOL_CALL_RESULT]...
  │    → 回到 THINKING（下一轮）
  └─ 纯文本:
       PROVISIONAL_COMMIT(all drafts)
       → TEXT_DELTA
       → 函数返回
```

---

## 五、流式传输与 Provisional 协议

### 5.1 问题

流式传输 LLM 输出时，文本 delta 立即推送给前端。但如果 LLM 随后决定调用工具，之前推送的文本只是"思考过程的前言"，不应成为最终回复。

### 5.2 Provisional 生命周期

```
            LLM 开始流式输出
                   │
                   ▼
    ┌─── PROVISIONAL_TEXT_DELTA ───┐
    │     (provision_id = uuid)    │
    │     前端：灰色/斜体渲染      │
    └──────────────────────────────┘
                   │
          ┌───────┴───────┐
          │               │
     有工具调用       纯文本完成
          │               │
          ▼               ▼
   PROVISIONAL_RETRACT  PROVISIONAL_COMMIT
   (前端：移除文本)     (前端：正式显示)
                          │
                          ▼
                     TEXT_DELTA
                  (最终确认的文本)
```

### 5.3 provision_id 的作用

- 每次 LLM 调用生成一个 `uuid4().hex` 作为 provision_id
- 所有该次调用的流式 text delta 共享同一个 provision_id
- commit / retract 按 provision_id 操作
- 支持**跨续写的累积**：length-continuation 会产生多个 provision_id，全部需要 commit 或全部 retract

### 5.4 流式降级

```python
# 先尝试流式
stream_events = getattr(llm, "chat_events", None)
if getattr(llm, "stream", False) and callable(stream_events):
    try:
        async for event in stream_events(conversation, tools=tools):
            ...
    except Exception:
        if saw_content_event or active_provision_ids:
            # 已推送内容 → 不能降级，只能报错
            retract all provisionals
            raise
        # 没推送过任何内容 → 安全降级到非流式
        response = await llm.chat(conversation, tools=tools)
else:
    response = await llm.chat(conversation, tools=tools)
```

**降级条件**：流式失败 **且** 尚未向前端推送过任何文本/工具内容。一旦推送过，降级会导致前端状态不一致（前半截流式、后半截非流式），所以只能 retract + raise。

### 5.5 `_ProviderResponseAccumulator`

流式模式下，provider 事件需要重新组装成 `ChatResponse`（与非流式接口统一）：

| Provider 事件 | 累积到 |
|---|---|
| `OUTPUT_TEXT_DELTA` | `text_parts[]` |
| `REASONING_DELTA` | `reasoning_parts[]` |
| `FUNCTION_CALL_ARGUMENTS_DELTA` | `tool_calls[index].argument_parts[]` |
| `USAGE` | `usage` |
| `RESPONSE_COMPLETED` | `finish_reason`, `raw_finish_reason` |

`build()` 方法在流完成后组装为 `ChatResponse`。特殊处理：
- `finish_reason=length` 时工具调用的 JSON 可能被截断 → 用 `__incomplete_tool_call__` 占位，不执行
- 流中断导致 tool_call 缺 id/name → 抛 RuntimeError
- arguments 解析失败 → 抛 RuntimeError

---

## 六、预算与终止条件

所有预算常量定义在 `engine/execution/react/budget.py`：

### 6.1 预算矩阵

| 预算 | 常量 | 值 | 计数逻辑 |
|---|---|---|---|
| 工具调用总轮数 | `DEFAULT_MAX_REACT_ITERS` | 60 | 每轮至少有 1 个成功工具调用 → productive_iters++ |
| 工具失败恢复 | `MAX_FAILED_TOOL_RECOVERY_ITERS` | 20 | 整轮全失败 → recovery_iters++ |
| Preflight 挑战 | `MAX_PREFLIGHT_CHALLENGE_ITERS` | 20 | 整轮全被 FactGate challenge → preflight_iters++ |
| 相同错误短路 | `MAX_IDENTICAL_TOOL_ERRORS` | 6 | 同 tool_name + 同 error_content[:120] 连续出现 |
| 输出超限续写 | `MAX_LENGTH_CONTINUATIONS` | 2 | finish_reason=length 时自动续写 |
| 假完成修复 | `MAX_INCOMPLETE_FINAL_REPAIRS` | 2 | LLM 回复只说"让我去查"而非给答案 |

### 6.2 运行时控制提示注入

运行时控制文本统一由 `engine/execution/runtime_control.py` 生成，ReAct 只在状态变化时把它作为 system 消息追加。连续 3 次工具失败后，向对话注入 `TOOL_FAILURE_HINT`：

> "Multiple tool calls have failed consecutively. Change your approach - try a different tool, simplify the command, or explain what you need without using tools."

注入后计数器归零。这是一种**柔性干预**——不终止循环，而是提示 LLM 换策略。

工具被 `ToolPolicy` 阻断时，工具观察仍保留 `[BLOCKED]` 原因；随后追加“不绕过、不重复同一副作用操作、改走安全替代方案或如实说明限制”的控制指令。该提示帮助模型选择下一步，但是否执行始终由 `ToolPolicy` / `ToolGuard` 决定。

### 6.3 相同错误短路

如果 LLM 反复调用同一个工具且报同一个错误（`tool_name:error_content[:120]` 为 key），连续 6 次后直接终止。防止 LLM 陷入"重试同一个失败命令"的死循环。

### 6.4 "假完成"修复

LLM 在执行完工具后，有时会回复"让我去搜索一下"而非给出真正的总结。`looks_like_incomplete_final_after_tool()` 通过正则检测这种模式（中英文都覆盖），然后注入运行时控制指令，要求 LLM 给出真正的最终答案和交付信息。

检测条件：
1. 之前有过成功的工具调用（`had_successful_tool`）
2. 修复次数 < 2
3. 文本匹配"下一步动作"模式（≤ 240 字符）

---

## 七、会话压缩

`engine/context/compression.py` 提供三级压缩，每轮循环开头执行：

### 7.1 第一级：工具输出裁剪 (`prune_tool_outputs`)

```
保护最近 2 个用户轮次内的工具输出
超过 8000 字符保护阈值后 → 旧工具输出替换为 "[pruned]"
裁剪量 < 2000 字符 → 跳过（不值得）
```

特点：**原地修改** conversation 列表。因此 `react_event_loop` 在进入循环前对 messages 做了浅拷贝（`[dict(m) for m in messages]`），防止污染调用方的原始数据。

### 7.2 第二级：LLM 摘要压缩 (`compact_history`)

当工具裁剪后 token 数仍超过上下文窗口的 70%（`CONTEXT_TRIGGER_RATIO`）时，用 LLM 将整个对话压缩为结构化摘要：

```xml
<context_summary>
  <conversation_overview>...</conversation_overview>
  <key_knowledge>...</key_knowledge>
  <file_system_state>...</file_system_state>
  <recent_actions>...</recent_actions>
  <current_plan>...</current_plan>
</context_summary>
```

压缩后对话变为 3 条消息：`[system_prompt, summary, ack]`。

### 7.3 第三级：硬裁剪

如果压缩后对话仍超过 `CONVERSATION_HARD_LIMIT`（40 条消息），直接截断：

```python
head = conversation[:2]   # system + 初始 user
tail = conversation[-28:]  # 最近 28 条
conversation = head + tail
```

这是最后的安全网，防止 token 溢出。

### 7.4 Token 估算

```python
def estimate_tokens(text):  # engine/context/budget.py:35
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    return (3 * cjk) + (len(text) - cjk + 2) // 3
```

CJK 字符按 3:1 保守估算（一个汉字 UTF-8 编码 3 字节，作为无合并 token 时的稳定上界），
其余按 3:1。宁可高估触发压缩，不要低估撑爆窗口。

---

## 八、工具策略集成

### 8.1 ToolPolicy — 统一策略网关

```python
class ToolPolicy:
    def evaluate(call) -> ToolPolicyDecision:
        1. ToolGuard.check()  → blocked (权限拦截)
        2. FactGate.evaluate() → challenged (事实挑战)
        3. 都通过 → allowed
```

### 8.2 三种决策结果在循环中的处理

| 决策 | 处理 | 计入 |
|---|---|---|
| `allowed` | 执行工具，结果入对话 | 成功 → productive_iters；失败 → consecutive_errors |
| `blocked` | `[BLOCKED] reason` 入对话，不执行 | round_had_failure → recovery_iters |
| `challenged` | `[PREFLIGHT] reason` 入对话，不执行 | round_had_preflight → preflight_iters |

### 8.3 轮次分类逻辑

一轮可能包含多个工具调用。按优先级分类：

```python
if round_had_success:
    productive_iters += 1    # 至少一个成功 → 算有效轮
elif round_had_failure:
    recovery_iters += 1      # 全失败 → 算恢复轮
elif round_had_preflight:
    preflight_iters += 1     # 全被挑战 → 算 preflight 轮
```

---

## 九、异常传播

### 9.1 事件 → 异常映射（文本/流式适配器）

```
INCOMPLETE 事件 → IncompleteAgentRunError(reason)
FAILED 事件    → FailedAgentRunError(reason)
正常返回        → str / Generator[str]
```

### 9.2 异常层级

```
RuntimeError
  ├── IncompleteAgentRunError  # 软失败：预算耗尽、模型超限、内容过滤
  └── FailedAgentRunError      # 硬失败：provider 错误
```

上层（pipeline / agent_loop / session_service）可以区分处理：
- `IncompleteAgentRunError` → 可能向用户展示部分结果
- `FailedAgentRunError` → 需要重试或报错

### 9.3 流式异常处理

Provider 流中断时：
- 如果还没推送过任何内容 → 降级到非流式（静默恢复）
- 如果已推送过内容 → retract 所有 provisional → 向上层抛异常

Accumulator build 失败时（JSON 解析错误等）：
- retract 所有 provisional → 向上层抛异常

---

## 十、设计权衡与已知局限

### 10.1 选择局部变量而非状态类

状态全部是 `react_event_loop` 的局部变量（~15 个计数器/标志），没有抽到 dataclass。

- **优势**：函数结束即销毁，不可能出现状态泄露；无需管理生命周期
- **代价**：函数体 ~340 行，超出通常的"一屏"阈值
- **升级路径**：如果计数器继续增长，抽为 `_LoopState` dataclass

### 10.2 浅拷贝 vs 深拷贝

进入循环前对 messages 做 `[dict(m) for m in messages]`（浅拷贝）。如果 message 的 value 是嵌套 dict/list，修改仍会影响调用方。当前 compress 只替换顶层 `content` 字段，浅拷贝够用。

### 10.3 Conversation 硬裁剪的信息丢失

硬裁剪保留 head 2 + tail 28，中间段直接丢弃。丢弃的部分不经过 LLM 摘要，可能丢失关键上下文。这是"宁可丢信息也不撑爆窗口"的取舍——正常流程中 LLM 摘要压缩应先于硬裁剪触发。

### 10.4 单线程工具执行

一轮中的多个工具调用是**顺序执行**的（`for tc in response.tool_calls`），不做并行。

- **优势**：工具执行顺序确定，错误归因清晰
- **代价**：多工具轮次的延迟是各工具延迟之和
- **升级路径**：`asyncio.gather` 并行执行，但需处理工具间依赖

### 10.5 finish_reason 的防御性处理

对 `finish_reason` 的每个可能值都有显式分支，包括未知值（映射为 `"other"`）。这是因为不同 LLM Provider 返回的 finish_reason 值不一致（OpenAI 用 `"stop"`, 有些用 `"max_tokens"` 而非 `"length"`），统一在 `normalize_finish_reason()` 层处理。

### 10.6 Provisional 协议的前端耦合

Provisional 生命周期假设前端能够：
1. 按 provision_id 暂存草稿文本
2. 收到 commit 时提升为正式文本
3. 收到 retract 时移除草稿

如果前端不支持 provisional（如简单日志 consumer），忽略 `PROVISIONAL_*` 事件只消费 `TEXT_DELTA` 即可——`TEXT_DELTA` 在 commit 之后总会补发完整文本。

---

## 附录：文件地图

| 文件 | 行数 | 职责 |
|---|---|---|
| `engine/execution/react/react_loop.py` | 1170 | 核心循环 + 流式组装 + 适配器 |
| `engine/execution/react/budget.py` | 124 | 预算常量 + 假完成检测 + 预算耗尽兜底 |
| `engine/execution/events.py` | 100 | ExecutionEvent + EventType 枚举 |
| `engine/context/compression.py` | 283 | 工具裁剪 + LLM 摘要压缩 |
| `engine/context/budget.py` | 170 | token 估算 + 上下文预算策略 |
| `engine/safety/tool_policy.py` | 163 | ToolGuard + FactGate 统一网关 |
| `engine/llm/events.py` | 64 | ProviderEvent + ProviderEventType 枚举 |
