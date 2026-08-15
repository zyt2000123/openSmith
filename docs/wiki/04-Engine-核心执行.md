# 04 · Engine 核心执行

> **定位**：`engine/` 里占 7.6k 行的执行子系统——上下文怎么装配、ReAct 循环怎么转、工具怎么注册和执行、管线与门禁怎么推进。
> **适合**：要改执行行为的人；想知道"为什么模型没看到我以为它能看到的东西"的人。

这一篇覆盖 deepwiki 分类 4 的全部五个子项：ReAct Loop、Orchestration Lifecycle、Pipeline and Skill Chain、Tool Registry and Execution、Context Assembly and Compression。

---

## 1. 全景

```mermaid
flowchart TD
    subgraph 装配["装配阶段（每轮一次）"]
        AS["assembler.py<br/>16 层，裁剪，渲染"] --> BD["budget.py<br/>估 token"]
        BD --> FT["fitting.py<br/>fit_request()"]
        FT --> CP["compression.py<br/>模型压缩 / summary.py"]
    end
    subgraph 循环["ReAct 循环（每轮多次）"]
        RL["react_loop.py<br/>1386 行"]
        RL --> PV["provider 流式或非流式"]
        PV --> TC{"有 tool_calls"}
        TC -->|"是"| TP["tool_policy.py<br/>ToolPolicy.evaluate()"]
        TP --> TG["tool_guard 硬守卫"]
        TP --> FG["fact_gate 软挑战"]
        TP --> HK["PreToolHook"]
        HK --> TR["registry.execute()"]
        TR --> PH["PostToolHook"]
        PH --> RL
        TC -->|"否"| FIN["最终回答"]
    end
    subgraph 管线["管线（可选外层）"]
        PL["pipeline.py<br/>run_pipeline()"] --> SC["skill_chain.py<br/>节点，门禁，条件"]
        SC --> GT["gate.py<br/>启发式 + LLMGate"]
        GT -->|"retry"| SC
        GT -->|"fail 多次"| BT["backtrack.py<br/>FailureLoopGuard"]
    end

    装配 --> 循环
    管线 --> 循环
```

三层的关系：**管线是可选外层，ReAct 是内核，装配在每次进模型前跑一次。**

---

## 2. 上下文装配：16 层可信标签 prompt

`engine/context/assembler.py`（792 行）把 system prompt 拆成 16 个可审计的层。这不是为了好看——每一层都带着五个正交的元数据维度，供裁剪、缓存、审计三个下游消费。

### 2.1 完整层清单

按渲染顺序：

| # | `name` | 显示名 | `source` | `authority` | `trust` | `scope` | `trim_priority` |
|---|---|---|---|---|---|---|---|
| 1 | `role` | Agent Role | AGENT_PROFILE | AGENT_POLICY | CONFIGURED | AGENT | **必留** |
| 2 | `style` | Agent Style | AGENT_PROFILE | AGENT_POLICY | CONFIGURED | AGENT | 50 |
| 3 | `workflow` | Agent Workflow | AGENT_PROFILE | AGENT_POLICY | CONFIGURED | AGENT | **必留** |
| 4 | `toolbox_policy` | Tool Usage Policy | AGENT_PROFILE | AGENT_POLICY | CONFIGURED | AGENT | 40 |
| 5 | `tool_definitions` | Available Tools | TOOL_REGISTRY | CAPABILITY | CONFIGURED | AGENT | 35 |
| 6 | `skills` | Available Skills | SKILL_REGISTRY | CAPABILITY | CONFIGURED | AGENT | 30 |
| 7 | `learned_context` | Learned User Context | LEARNED_CONTEXT | REFERENCE | **UNTRUSTED_REFERENCE** | USER | **必留** |
| 8 | `global_instructions` | Global Instructions | SMITH_FILE | USER_POLICY | USER_AUTHORED | USER | **必留** |
| 9 | `project_instructions` | Project Instructions | SMITH_FILE | PROJECT_POLICY | USER_AUTHORED | PROJECT | **必留** |
| 10 | `identity_guidance` | Identity Guidance | IDENTITY_CATALOG | AGENT_POLICY | CONFIGURED | AGENT | **必留** |
| 11 | `eval_guidance` | Evaluation Safety Guidance | ENGINE | ENGINE_CONTROL | TRUSTED | AGENT | **必留** |
| 12 | `output_style` | Output Style | ENGINE | AGENT_POLICY | CONFIGURED | AGENT | 10 |
| 13 | `memory_governance` | Memory Governance | ENGINE | ENGINE_CONTROL | TRUSTED | AGENT | **必留** |
| 14 | `durable_context` | Durable Memory | MEMORY_DURABLE | REFERENCE | **UNTRUSTED_REFERENCE** | PROJECT | 19 |
| 15 | `runtime_context` | Runtime Context | RUNTIME | RUNTIME_FACT | AUTHORITATIVE_FACT | RUNTIME | **必留** |
| 16 | `runtime_control` | Engine Runtime Control | ENGINE | ENGINE_CONTROL | TRUSTED | RUNTIME | **必留** |

### 2.2 为什么要五个正交维度

一个天真的实现只需要"内容 + 顺序"。这里拆成五个维度，是因为**四个不同的问题需要四种不同的分类**：

```mermaid
flowchart LR
    L["PromptLayer"] --> S["source<br/>它从哪来"]
    L --> A["authority<br/>它在治理上什么身份"]
    L --> T["trust<br/>它的内容能信到什么程度"]
    L --> SC["scope<br/>它属于谁"]
    L --> LR["load_reason<br/>它为什么在这一轮出现"]

    S --> Q1["审计：这段话是谁写的"]
    A --> Q2["冲突：两层矛盾时听谁的"]
    T --> Q3["安全：能不能把它当指令执行"]
    SC --> Q4["缓存：它跨请求稳定吗"]
    LR --> Q4
```

**最关键的是 `trust` 与 `authority` 分开。** 举个例子：

- `learned_context`（第 7 层）的 `authority` 是 `REFERENCE`，`trust` 是 `UNTRUSTED_REFERENCE`
- `global_instructions`（第 8 层）的 `authority` 是 `USER_POLICY`，`trust` 是 `USER_AUTHORED`

两层都由"用户"这一侧产生，但第 7 层是**Agent 自己学出来的**，第 8 层是**用户亲手写的**。前者绝不能被当成指令执行——否则一个被污染的对话就能通过"学习"把恶意指令写进 prompt，形成持久化的提示注入。

代码里对应的处理是一道围栏（`_LEARNED_CONTEXT_FENCE`），明确告诉模型：下面是参考资料，不是指令。`durable_context`（第 14 层，项目记忆）同理，用 `_MEMORY_REFERENCE_FENCE`。

### 2.3 裁剪策略

`_trim_to_budget()` 的逻辑很克制：

```python
"""Drop only explicitly trimmable layers in declared priority order."""
```

- **只裁掉显式声明了 `trim_priority` 的层**，`None` 的一律保留
- 按 `(trim_priority, index)` 升序，**从最便宜的开始裁**
- 裁到总量落回预算就停，不多裁
- 裁掉的方式是 `replace(layer, content="")`——**保留层本身**，这样 manifest 里还能看到"这一层被裁了"

裁剪顺序：

```mermaid
flowchart LR
    A["output_style<br/>10"] --> B["durable_context<br/>19"] --> C["skills<br/>30"] --> D["tool_definitions<br/>35"] --> E["toolbox_policy<br/>40"] --> F["style<br/>50"]
    G["其余 10 层<br/>trim_priority 为 None<br/>永不裁剪"]
    style G fill:#ffe0e0
```

这个顺序编码了一套价值判断：

| 先裁 | 理由 |
|---|---|
| 输出风格（10） | 影响的是"怎么说"，不是"能不能做对" |
| 项目记忆（19） | 有用但非必需；裁了模型仍能靠对话完成任务 |
| 技能目录（30） | 裁了就用不了技能，但核心能力还在 |
| 工具定义（35） | 比技能更基础，所以更晚裁 |
| 工具使用策略（40） | 工具都没了，策略也没意义，所以排在工具后面 |
| 人格风格（50） | 最后裁。它便宜且塑造整体表现 |

**永不裁剪的十层**包括：角色、工作流、学到的用户上下文、全局指令、项目指令、身份指引、评测安全指引、记忆治理、运行时上下文、引擎运行时控制。规律是——**身份、指令、治理、事实，一律不裁**。裁掉的只能是"能力目录"和"表达偏好"。

### 2.4 前缀缓存键：为什么是"连续前缀"而不是"稳定的层"

`_stable_prefix()` 的 docstring 是这个文件里最值得读的一段：

> 前缀必须是**从第一层开始连续的**，因为 provider 的 prompt 缓存按**字节前缀**匹配：第一个与请求相关的层就终结了前缀，不管它后面的层多稳定。按层的语义而不是固定切片来选，意味着插入或重排一个层不会静默移动这个边界。

判定"易变"的规则：

```python
_VOLATILE_PROMPT_SOURCES = {MEMORY_STORE, MEMORY_DURABLE, RUNTIME}
_VOLATILE_LOAD_REASONS  = {QUERY_RETRIEVAL, EVAL_SENSITIVE, RUNTIME_ONLY}
```

代入 16 层清单：第 11 层 `eval_guidance` 的 `load_reason` 是 `EVAL_SENSITIVE`，所以**稳定前缀就是第 1 到 10 层**（role 到 identity_guidance）。

```mermaid
flowchart LR
    subgraph 稳定前缀["稳定前缀（进 provider 缓存）"]
        direction LR
        P1["1 role"] --> P2["2 style"] --> P3["3 workflow"] --> P4["4 toolbox"] --> P5["5 tools"] --> P6["6 skills"] --> P7["7 learned"] --> P8["8 global"] --> P9["9 project"] --> P10["10 identity"]
    end
    subgraph 易变区
        V11["11 eval_guidance<br/>EVAL_SENSITIVE"] --> V12["12 output_style"] --> V13["13 memory_governance"] --> V14["14 durable<br/>MEMORY_DURABLE"] --> V15["15 runtime_context<br/>RUNTIME"] --> V16["16 runtime_control<br/>RUNTIME_ONLY"]
    end
    稳定前缀 --> 易变区
    style 稳定前缀 fill:#e8f5e9
    style 易变区 fill:#fff4e6
```

还有一处细节：缓存键**绑定的是裁剪之后实际渲染的内容**，不是裁剪前的源材料。

```python
# Bind the routing hint to the stable prefix that is actually rendered,
# not to pre-trim source material that the provider never receives.
```

而且空层被跳过——因为"键必须哈希 provider 真正看到的字节"。这两点决定了缓存命中率是不是真的。

### 2.5 sanitize 失败时不许静默

第 7 层的处理里有一段很典型的工程判断：

```python
cleaned, secrets_removed, injections_removed = sanitize_memory_text(learned_context)
if not cleaned.strip() and (secrets_removed or injections_removed):
    # Dropping the layer is the safe answer, but doing it silently
    # is indistinguishable from "nothing learned yet" — for every
    # turn from here on. Say it instead.
    _log.warning("context.md was dropped from the prompt: ...")
```

**丢弃是对的，但静默丢弃是错的**——从此以后每一轮，"这份上下文被安全策略整份丢掉了"和"还没学到任何东西"在行为上完全一样，用户永远不会发现。

### 2.6 装配产物：三个对象

```mermaid
classDiagram
    class AssembledPrompt {
        +str text
        +tuple layers
        +PromptManifest manifest
        +str prefix_cache_key
        +PromptPlan plan
    }
    class PromptManifest {
        +str rendered_prompt_hash
        +tuple layers
        +dict budget
        +to_trace_data()
    }
    class PromptPlan {
        +int token_budget
        +int source_tokens
        +int required_tokens
        +int rendered_tokens
        +bool within_budget
        +tuple trimmed_layers
    }
    AssembledPrompt --> PromptManifest
    AssembledPrompt --> PromptPlan
```

`PromptManifest` 的定义写着"**redacted receipt**，可安全存入运行 trace"——它记的是层名、来源、token 数、哈希，**不记内容**。这样运行档案里能回答"这一轮的 prompt 由哪些层组成、哪些被裁了"，又不会把用户的私有指令抄进日志。

`PromptPlan.trimmed_layers` 的算法也很直白：**源层有内容而渲染层为空**的，就是被裁掉的。

---

## 3. 上下文预算与拟合

### 3.1 一个被中文掩盖的宽字符 bug

`engine/context/budget.py` 里 `_WIDE_CHAR_RANGES` 上方的注释，是整个仓库里最好的一条 bug 记录：

> 把这个范围限制在 CJK 统一表意文字（U+4E00-U+9FFF）会把每个假名、谚文音节、CJK/全角标点算成 1/3 个 token 而不是约 1 个——对日文和韩文输入构成**系统性的 3 倍低估**，而下面每一个预算都是从这个数推导出来的。**中文把它藏住了**：表意文字上的 3 倍高估抵消了 `。、「」` 上的低估。

这是一类特别难发现的 bug：**用中文测试永远测不出来**，因为两个方向的误差刚好抵消。修法是把范围扩到七段：

| 范围 | 覆盖 |
|---|---|
| U+3000–U+303F | CJK 符号与标点 |
| U+3040–U+30FF | 平假名 + 片假名 |
| U+3400–U+4DBF | CJK 扩展 A |
| U+4E00–U+9FFF | CJK 统一表意文字 |
| U+AC00–U+D7AF | 谚文音节 |
| U+F900–U+FAFF | CJK 兼容表意文字 |
| U+FF00–U+FFEF | 半角/全角形式 |

估算公式：

```python
def estimate_tokens(text: str) -> int:
    wide = sum(1 for char in text if _is_wide_char(char))
    return (3 * wide) + (len(text) - wide + 2) // 3
```

即 **CJK 字符按 3 token，其它按 3 字符 1 token**。docstring 说明了为什么取 3：一个 CJK 字符是三个 UTF-8 字节，这是"provider 的分词器对该字符没有合并 token 时"的稳定上界。**保守估计，宁可高估**。

还有一个性能细节：ASCII 与拉丁文本在 `char < "　"` 处直接短路，不进七段扫描。

### 3.2 预算三常量

```python
CONTEXT_COMPACTION_TRIGGER = 128_000
CONTEXT_SAFETY_MARGIN_RATIO = 0.10
CONTEXT_COMPACTION_INPUT_RATIO = 0.85
```

`ContextBudget` 把它们展开成八个字段：

| 字段 | 含义 |
|---|---|
| `model_context_window` | 模型声明的窗口 |
| `effective_context_window` | 扣掉不可用部分后的有效窗口 |
| `output_reserve` | 给输出预留的额度 |
| `safety_margin` | 安全余量（10%） |
| `safe_input_budget` | 输入的安全上限 |
| `compaction_trigger` | 触发压缩的阈值 |
| `window_declared` | 窗口是**声明的**还是猜的 |
| `output_limit_declared` | 输出上限是声明的还是猜的 |

最后两个布尔值是设计亮点：**区分"我知道这个值"和"我用了默认值"**。一个没声明 `context_window` 的模型，预算计算必须更保守，而下游需要知道自己在保守还是在精确。

### 3.3 `fit_request()`

`engine/context/fitting.py`（269 行）负责让请求装进预算。它返回一个带 `status` / `actions` / `receipt` / `fits` 的结果，ReAct 循环据此决定：

```python
if any(action in {"compaction_failed", "compaction_rejected"} for action in fit.actions):
    model_compaction_enabled = False
```

**压缩失败或被拒一次，之后就不再尝试模型压缩**。因为一个压缩不了的上下文再压一次大概率还是压不了，反复尝试只是烧钱。

而提交 `b0ef6a7 fix(observability): record what context fitting dropped, not just that it did` 说明了另一个原则：**只记录"发生了压缩"不够，必须记录"丢了什么"**。

---

## 4. ReAct 循环

`engine/execution/react/react_loop.py`（1386 行）是全仓库最大的文件。

### 4.1 循环全景

```mermaid
flowchart TD
    S["进入循环<br/>productive_iters 小于 max_iters"] --> A{"对话超过 40 条"}
    A -->|"是"| B["保留头 2 条加尾 28 条<br/>切点必须落在 assistant/user 边界"]
    A -->|"否"| C
    B --> C["measure_request()<br/>估算当前请求成本"]
    C --> D{"超过 compaction_trigger"}
    D -->|"是"| E["CONTEXT_COMPRESSION_START"]
    D -->|"否"| F
    E --> F["fit_request()"]
    F --> G{"fit.fits"}
    G -->|"否"| H["撤回所有草稿<br/>INCOMPLETE context_capacity_exhausted<br/>结束"]
    G -->|"是"| I["THINKING 事件"]
    I --> J{"流式可用"}
    J -->|"是"| K["chat_events() 流式<br/>累积并逐 delta 发 provisional"]
    J -->|"否"| L["chat() 非流式"]
    K -->|"异常"| M{"是上下文超限错误"}
    M -->|"是且首次"| N["撤回草稿，恢复上下文，continue"]
    M -->|"否"| O{"已见语义 delta 或有活跃草稿"}
    O -->|"是"| P["撤回草稿并抛出"]
    O -->|"否"| L
    K --> Q["TOKEN_USAGE 与 CONTEXT_USAGE"]
    L --> Q
    Q --> R{"response.has_tool_calls"}
    R -->|"是"| T["撤回草稿 tool_call_pending<br/>清空 final_text_parts"]
    R -->|"否"| U["最终回答路径"]
    T --> V["ToolPolicy.evaluate() 逐个工具"]
    V --> W["PreToolHook，execute，PostToolHook"]
    W --> S
    U --> X{"looks_like_incomplete_final_after_tool"}
    X -->|"是且修复次数未用尽"| Y["追加修复提示，continue"]
    X -->|"否"| Z["提交草稿，DONE"]
```

### 4.2 预算常量全表

`engine/execution/react/budget.py` 的常量决定了这个循环的所有边界：

| 常量 | 值 | 作用 |
|---|---|---|
| `DEFAULT_MAX_REACT_ITERS` | 60 | 有效迭代上限（工具调用轮数） |
| `MAX_FAILED_TOOL_RECOVERY_ITERS` | 20 | 工具失败后的恢复轮数上限 |
| `MAX_PREFLIGHT_CHALLENGE_ITERS` | 20 | 事实门挑战的轮数上限 |
| `MAX_INCOMPLETE_FINAL_REPAIRS` | 2 | "假最终回答"最多修两次 |
| `MAX_LENGTH_CONTINUATIONS` | 2 | 因长度截断的续写最多两次 |
| `CONVERSATION_HARD_LIMIT` | 40 | 对话消息数硬上限 |
| `CONVERSATION_KEEP_RECENT` | 28 | 裁剪时保留的尾部条数 |
| `CONVERSATION_KEEP_HEAD` | 2 | 裁剪时保留的头部条数（system 加首条 user） |
| `MAX_IDENTICAL_TOOL_ERRORS` | 6 | 同一个工具错误重复上限 |

注意 `productive_iters` 与 `recovery_iters` / `preflight_iters` 是**分开计数**的。这意味着"因为工具失败而多跑的轮次"不占用正常的 60 次预算——失败恢复有自己的账本。

预算耗尽时的消息统一由 `budget_exhausted_message()` 生成：

> "……我停下来以避免无限循环。请用更窄的请求重试，或检查最近一次失败的工具结果。"

**告诉用户下一步能做什么**，而不只是报告失败。

### 4.3 对话裁剪：一个 40 行的边界地狱

这段代码带着三段中文注释，每段都记录一个被修掉的 bug：

**① 切点不能落在 tool 结果串中间。**

```python
# 切点落在 tool 结果串中会拆散 assistant(tool_calls)/tool 配对（provider 400）。
```

OpenAI 兼容协议要求 `assistant(tool_calls)` 和它的 `tool` 结果成对出现。切在中间会得到孤儿 `tool` 消息，provider 直接 400。

**② 只认 `role == "tool"` 会在提示处停下。**

```python
# 同一轮的 tool 结果之间可能夹着 system 提示（TOOL_FAILURE_HINT 在 tool_calls
# 循环内 append），只认 role=="tool" 会在提示处停下留下孤儿。
```

所以回退条件是 `role in ("tool", "system")`。

**③ 向前回退撞到 head 边界时，这道上限会静默失效。**

```python
# 回退撞到 head 边界时 head+tail 就是整条对话，这道上限静默失效：
# 一轮 8 个并行工具调用即可（实测 41 条裁剪后仍是 41 条，30 个调用时 63 条原样返回）。
# 改为向后找下一个边界 —— 丢弃的更多，但配对完整且一定有进展。
```

这条注释里带了**实测数字**（41 条裁剪后仍是 41 条、30 个调用时 63 条原样返回），这是好 bug 记录的标志：它证明了作者真的复现过。

最终策略：

```mermaid
flowchart TD
    A["requested_cut = len 减 28"] --> B["向前回退<br/>跳过 tool 与 system"]
    B --> C{"回退到 head 边界了"}
    C -->|"否"| D["用这个切点"]
    C -->|"是"| E["改为向前找<br/>下一个非 tool/system 边界"]
    E --> F{"找到"}
    F -->|"是"| G["用它：丢得更多但一定有进展"]
    F -->|"否"| H["保持原样<br/>交给 fit_request 兜底"]
```

裁完之后还有一步：

```python
while head and head[-1].get("role") == "assistant" and head[-1].get("tool_calls"):
    head.pop()
```

**头部末尾如果是一个发起了工具调用的 assistant 消息，也要弹掉**——否则它的 tool 结果在尾部被裁掉了，同样是孤儿。

### 4.4 流式失败什么时候能回退到非流式

这是一个很容易做错的地方。代码的判据是：

```python
if saw_content_event or active_provision_ids:
    # 撤回草稿并抛出——不回退
    raise
# 否则回退到 llm.chat()
```

`saw_content_event` 的定义包括三种 provider 事件：文本 delta、**推理 delta**、函数调用参数 delta。注释解释了为什么推理 delta 也算：

> 一个流式的推理 delta 对用户不可见，但它**仍然证明 provider 已经开始生成响应**。回退会重放那一轮，并可能产生**不同的工具计划**，所以只重试那些在任何语义响应 delta 之前就失败的流。

即：**已经开始生成就不能重放**，因为重放的结果可能和用户已经看到的（或系统已经准备执行的）不一致。

### 4.5 试探性草稿的生命周期

```mermaid
stateDiagram-v2
    [*] --> 无草稿
    无草稿 --> 流式中: 首个文本 delta，生成 provision_id
    流式中 --> 流式中: PROVISIONAL_TEXT_DELTA
    流式中 --> 已撤回: 出现 tool_calls
    流式中 --> 已撤回: 上下文超限恢复
    流式中 --> 已撤回: 流错误
    流式中 --> 已撤回: 上下文容量耗尽
    流式中 --> 已提交: 到达最终回答
    已撤回 --> 无草稿
    已提交 --> [*]
```

四种撤回原因各对应一个真实场景。最微妙的是**上下文超限恢复**那条：

```python
# The abandoned draft was streamed to the client but is being discarded by
# this recovery. Retract it before the retried stream runs, or both the old
# and new ids would be committed at finish and the client would keep
# rendering text that no longer exists.
```

不撤回的后果是：旧 id 和新 id 都在结束时被提交，客户端会一直渲染一段**已经不存在的文本**。

还有一处相反方向的陷阱，注释是中文的：

```python
# 上面的 has_tool_calls 分支已把草稿 retract（tool_call_pending），
# 屏幕上的流式文本已被消费方删除；这里若再打 already_streamed
# 标记，消费方会跳过渲染 → 文本落库但用户永远看不到。
```

**已经撤回过的文本，重发时不能再标 `already_streamed`**——否则消费方以为它已经在屏幕上了，跳过渲染，结果是数据库里有、屏幕上没有。

### 4.6 "假最终回答"检测

模型经常在调完工具后说一句"让我再查一下 X"然后就停了——这不是最终回答，是一个没兑现的承诺。`looks_like_incomplete_final_after_tool()` 用正则识别它：

```python
_NEXT_ACTION_VERBS_ZH = ("查","搜","抓","获取","打开","访问","确认","验证","看看","看一下")
_NEXT_ACTION_VERBS_EN = ("search","fetch","check","open","browse","look up","verify")

_INCOMPLETE_FINAL_PATTERNS = (
    re.compile(r"(让我|我将|我会|我需要|接下来|下一步|继续).{0,24}(" + "|".join(_NEXT_ACTION_VERBS_ZH) + r")"),
    re.compile(r"(let me|i'll|i will|i need to|next,?|going to).{0,48}(" + "|".join(_NEXT_ACTION_VERBS_EN) + r")"),
)
```

两条约束让它不至于误伤：

- **长度上限 240 字符**。一个长回答里出现"接下来我会验证"是正常的收尾说明，只有**短到只剩一句承诺**才算假最终回答。
- 中英文分开写正则，中文允许 24 字符间隔，英文允许 48——因为英文更长。

命中后最多修 2 次（`MAX_INCOMPLETE_FINAL_REPAIRS`），追加一段引擎自有的提示（`incomplete_final_repair_prompt()`）让模型继续。

这是一个**用正则做行为纠偏**的例子。它当然不完美，但它便宜、确定、可测——符合"能用确定性代码判定的绝不交给模型"这条取向。

### 4.7 懒加载工具 schema

```python
lazy_tool_schemas = bool(getattr(tool_registry, "lazy_tool_schemas", False))
if lazy_tool_schemas:
    # Tool descriptions live in the stable prompt prefix. The provider sees
    # only this tiny loader schema until the model explicitly asks for one
    # capability, keeping the large JSON contracts out of every initial turn.
    tools = [_tool_schema_loader_definition()] if tool_registry.list_tools() else None
```

设计很巧：

```mermaid
flowchart LR
    A["prompt 第 5 层<br/>Available Tools<br/>只有名字加一句描述"] --> B["模型知道有哪些工具"]
    B --> C["模型调 get_tool_schema<br/>要某个工具的完整参数"]
    C --> D["返回该工具的 JSON schema"]
    D --> E["模型再正式调用它"]
```

**工具的"存在"放在稳定前缀里（可缓存），工具的"参数契约"按需加载（不进每一轮）**。20 多个工具的完整 JSON schema 很大，全放进每轮请求既费 token，又会因为 schema 变化打断前缀缓存。

`server/app/services/engine_runtime.py` 里构建的运行时确实开了这个开关：`ToolRegistry(lazy_tool_schemas=True)`。

---

## 5. 工具注册与执行

### 5.1 工具元数据契约

`engine/tool/interface.py` 的 `ToolDefinition` 有 17 个字段，分三组：

**① 基础**

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 工具名 |
| `description` | str | 给模型看的一句话 |
| `parameters` | dict | JSON Schema |
| `hidden` | bool | 注册但不进模型可见集合 |

`hidden` 的注释值得注意：

> 运行时基础设施工具可以注册但保持在模型可见的默认工具集之外。**可见性是元数据，绝不是调用方维护的一份工具名列表。**

**② 安全元数据**

| 字段 | 类型 | 说明 |
|---|---|---|
| `opaque_command` | bool | 不透明命令串，需要抽路径加显式审批。目前只有 `shell` 声明 |
| `network_access` | bool | 会开网络连接。**网络能力总是需要审批**，哪怕操作看起来只读 |
| `path_args` | tuple | 哪些参数是路径 |
| `list_path_args` | tuple | 哪些参数是路径列表 |
| `is_write_tool` | bool | 是否写工具 |
| `permission_level` | str | `read` / `write` / `execute` / `destructive` |
| `approval_policy` | str | `never` / `policy` / `always` |
| `read_actions` | frozenset | 哪些 action 算只读（`git_ops` 用它区分 `status`/`diff` 和 `commit`） |

**③ 执行契约**

| 字段 | 默认 | 说明 |
|---|---|---|
| `timeout_seconds` | None | 超时 |
| `retryable` | False | 可重试 |
| `side_effect` | `none` | `none` / `write` / `external` / `destructive` |
| `idempotent` | False | 幂等 |
| `concurrency` | `safe` | `safe` / `serial` |
| `execution_environment` | `host` | `host` / `sandbox` / `either` |

五组枚举在 `registry.py` 里都有校验集合（`_VALID_PERMISSION_LEVELS` 等），非法值直接拒绝注册。

### 5.2 `declared_paths`：一个精妙的安全设计

`ToolCall` 里有一个字段带着这个仓库最长的注释之一。问题是这样的：

```mermaid
flowchart TD
    A["模型给的路径<br/>project/.git/config"] --> B["normalize_call 必须解析<br/>让守卫和 provider 摸同一个文件"]
    B --> C["但解析会抹掉符号链接的名字"]
    C --> D["一个是软链的 .git<br/>解析后路径里没有 .git 这一段"]
    D --> E["守卫的目录名规则匹配不上，绕过成立"]
    F["declared_paths<br/>保留调用方写的拼法<br/>绝对化但不解析"] --> G["FileGuard 同时检查两个视图"]
    E -.->|"被这个字段堵上"| G
```

注释原文：

> `ToolRegistry.normalize_call` 必须把完全解析的路径交给 provider，这样守卫和 provider 才摸同一个文件；但解析也**抹掉了安全守卫的目录成分规则所依赖的符号链接名字**——一个是符号链接的 `.git` 会解析成一个不含 `.git` 成分的路径。把声明时的拼法一并带上，让 `FileGuard` 能继续检查两个视图。

最后一句还澄清了它不参与调用指纹：

> 不是调用指纹的一部分：它派生自 `arguments`，从来不是独立输入。

**派生数据不能进指纹**——否则同一个调用会因为派生方式变化而产生不同的指纹。

### 5.3 `ToolPolicy`：链式检查与延迟审批

`engine/safety/tool_policy.py` 把 `ToolGuard` 和 `FactGate` 适配成同一个 `PolicyChecker` 协议，按顺序跑，**第一个阻断者获胜**。

但有一个例外分支，是这个文件的精华：

```python
if decision.approval_required:
    deferred_approval = ...   # 记下来，但继续跑后面的检查器
    continue
```

```mermaid
flowchart TD
    A["ToolPolicy.evaluate(call)"] --> B["检查器 1：ToolGuard"]
    B --> C{"结果"}
    C -->|"allowed"| D["检查器 2：FactGate"]
    C -->|"blocked 且非审批"| E["立即返回 blocked"]
    C -->|"需要审批"| F["记入 deferred_approval<br/>继续往下检查"]
    F --> D
    D --> G{"结果"}
    G -->|"challenged"| H["返回 challenged<br/>审批被推迟到重试"]
    G -->|"allowed"| I{"有 deferred_approval"}
    I -->|"是"| J["返回需要审批"]
    I -->|"否"| K["allowed"]
```

注释解释了为什么：

> 事实强制挑战**刻意优先于**后续的用户审批。第一次尝试必须先把要求的事实建立起来；只有重试才可以为审批而暂停。

翻译成场景：模型第一次要写文件，同时触发了"你还没做调查"（挑战）和"这是个危险写操作"（需审批）。正确的顺序是**先让它去调查**——因为调查完之后它可能根本不需要这次写操作了，那就不用打扰用户审批。反过来先弹审批，用户批准了一个基于错误理解的操作。

`evaluate()` 还会**沿途累积**最高权限等级和最高风险等级（`_LEVEL_ORDER` / `RiskTier.max`），让最终决定带上全链路的最严判定。

### 5.4 输出截断

`engine/tool/truncation.py`：

```python
MAX_LINES = 2000
MAX_BYTES = 50 * 1024   # 50KB
```

超限时**完整输出落盘**到 `~/.agent-smith/tool-output/`，返回值里带一个指向该文件的提示。这样模型能看到概要，需要细节时可以再读文件——而不是把 500KB 的日志塞进上下文。

### 5.5 `ScopedToolRegistry`

管线节点的 `allowed_tools` 通过 `registry.scoped_to(names)` 实现。它是一个**视图**，不是拷贝：

- `_active_names()` 每次动态算，所以底层注册表的变化能反映过来
- 但可见集合被限制在给定的名字里

这就是 [02 · 快速上手](./02-快速上手.md) §10.1 讲的三级收窄的第三级。

---

## 6. Pipeline 与技能链

### 6.1 门禁与条件是内容，不是引擎代码

`skill_chain.py` 顶部的注释把边界说得很清楚：

> 门禁和条件的实现是**内容，不是引擎代码**。注册表初始为空，由 `load_gate_content()` 填充，它扫描 `<agents_dir>/gates/**.py`（模块级 `GATES`）和 `<agents_dir>/conditions/**.py`（模块级 `CONDITIONS`）。`from_yaml` 保持为纯查找，遇到未知 key **大声失败**而不是静默降级。

加载机制和工具一样是 `exec_module`，但多了几处考究：

**① 注入能力而不是让内容 import 引擎。**

```python
mod.output_key = output_key
# Content files stay independent from engine imports.
# Expose the stable output-key helper as an injected capability instead.
```

内容文件需要 `output_key` 这个辅助函数，但让它 `from engine... import` 就破坏了内容层独立性。所以**引擎把函数塞进模块的命名空间**再执行它。

**② 执行前先注册进 `sys.modules`。**

```python
# Some declarative content uses standard decorators (for example dataclasses).
# They resolve annotations through sys.modules, so register the transient
# module before executing it.
sys.modules[module_name] = mod
```

`dataclass` 解析注解时要通过 `sys.modules` 找回自己的模块。不先注册，装饰器会失败。

**③ 失败一律清理。** 每个异常分支都有 `sys.modules.pop(module_name, None)`——半加载的模块留在 `sys.modules` 里会污染后续加载。

**④ 模块名用路径的 SHA-1**，避免不同目录下的同名文件互相覆盖。

**⑤ 重名策略分两层。**

- **同一个项目内**重名，抛 `GateContentError`，硬失败
- **跨项目**（回落到模块级全局注册表）用 `setdefault`，先到先得

注释说明了理由："这样另一个项目就不能覆盖一个已经选定的项目的工厂函数。"

### 6.2 门禁三态

```python
@dataclass
class GateResult:
    verdict: Literal["pass", "fail", "retry"]
    reason: str
    retry_hint: str | None = None
```

| 判定 | 含义 | 后续 |
|---|---|---|
| `pass` | 契约满足 | 进入下一节点 |
| `retry` | 差一点，给出具体提示 | 带 `retry_hint` 重跑本节点 |
| `fail` | 不满足 | 交给 `FailureLoopGuard` 决定重试/切换/阻断 |

`coerce_gate_result()` 负责把内容层返回的东西适配成 `GateResult`——支持 `GateResult` 实例、`Mapping`、或任何有这三个属性的对象。这样内容文件可以只返回一个 dict，不需要 import 引擎类型。

但它**不放过类型错误**：

```python
# ``GateResult`` is a plain dataclass whose ``Literal`` verdict is not
# runtime-enforced, so it must pass the same checks as every other shape.
if verdict not in {"pass", "fail", "retry"} or not isinstance(reason, str):
    raise TypeError(...)
```

即便传进来的已经是 `GateResult`，也要重新校验——因为 `Literal` 只是类型标注，运行时不强制。

### 6.3 `_bounded_gate_output`：一个"两层看到不同文本"的 bug

这是本文档里最值得单独讲的一个 bug：

```python
_MAX_GATE_OUTPUT_CHARS = 2000
```

`LLMGate` 是两层结构：**便宜的启发式预过滤加 LLM 语义验证**。bug 在于两层读的文本不一样：

```mermaid
flowchart TD
    A["节点输出很长<br/>证据在最后"] --> B["启发式预过滤<br/>扫描整个输出，看到证据"]
    A --> C["旧实现：只把前 2000 字符给 LLM<br/>看不到证据"]
    B --> D["预过滤：通过"]
    C --> E["LLM：证据在哪，拒绝"]
    D --> F["结果：一个输出其实正确的节点被拒"]
    E --> F
```

注释原文：

> 验证和评审节点把它们的证据放在**最后**——它们刚刚产出的命令输出——所以预过滤会确认一份 LLM 看不到的证据，于是门禁拒绝了一个输出其实正确的节点。

修法是**两端都留，并且说明中间被省略了**：

```python
marker = "\n[... middle of output omitted from gate input ...]\n"
head = available // 2
return f"{output[:head]}{marker}{output[-(available - head):]}"
```

注释还指出这和记忆模块的 `_review._truncate_source` 是同一套做法——**同一个问题在两个子系统里用同一个解法**，这是好架构的信号。

对应提交：`0eae0a0 fix(pipeline): let the gate LLM see the end of a long node output`。

### 6.4 `FailureLoopGuard`：三段升级

```python
class FailureLoopGuard:
    """Per-node failure escalation: bounded retries, then one switch, then block."""
    def __init__(self, max_same: int = 2, max_attempts: int = 3): ...
```

```mermaid
stateDiagram-v2
    [*] --> 第1次失败
    第1次失败 --> retry: attempts=1 小于 max_same=2
    retry --> 第2次失败
    第2次失败 --> switch: attempts=2 小于 max_attempts=3
    switch --> 第3次失败
    第3次失败 --> blocked: attempts=3
    blocked --> [*]
```

关键在 docstring 里的两条**否定**设计：

> 状态**只**按 `error_type`（失败的节点）作键。
> - 一个全局的策略集合会让**不相关节点**早先的失败过早地阻断当前节点。
> - 按输出哈希计数**永远不会终止**，因为 LLM 的输出在重试之间总是变化的。

第二条尤其值得记：**任何以"LLM 输出内容"为键的计数器都不会收敛**。

### 6.5 管线上下文与检查点

`pipeline.py` 的常量：

```python
_BASE_GATE_MAX_RETRIES = 3
CTX_PROVISIONAL_OUTPUTS = "_committed_provisional_output"
```

主要辅助函数：

| 函数 | 作用 |
|---|---|
| `_check_base_gates()` | 跑通用门禁（`agents/gates/common/`） |
| `_ends_with_user_question()` | 推断节点是否在提问（配合 `infer_await_user_input_from_question`） |
| `_collect_node_events()` | 收集一个节点的事件流 |
| `_validate_chain_topology()` | 校验链的拓扑（节点索引、门禁存在性） |
| `_evict_outputs_at_or_after()` | 回退时清掉该节点及之后的产物 |
| `_is_budget_message()` | 识别预算耗尽消息，避免把它当成正常产出 |
| `_save_checkpoint()` / `_clear_checkpoint()` | 检查点读写 |

`_evict_outputs_at_or_after()` 是回退正确性的关键：回退到节点 2 时，节点 2/3/4 的产物必须清掉——否则节点 3 会读到上一轮的陈旧产物。

`_is_budget_message()` 的存在同样重要：ReAct 预算耗尽时会产出一段说明文字，如果门禁把它当成节点的正常输出去判定，会得到莫名其妙的结论。

---

## 7. 参数速查

### ReAct

| 参数 | 值 |
|---|---|
| 最大有效迭代 | 60 |
| 工具失败恢复上限 | 20 |
| 事实门挑战上限 | 20 |
| 假最终回答修复上限 | 2 |
| 长度截断续写上限 | 2 |
| 对话硬上限 / 保留尾 / 保留头 | 40 / 28 / 2 |
| 同一工具错误重复上限 | 6 |
| 上下文超限恢复次数 | 1 |

### 上下文

| 参数 | 值 |
|---|---|
| Prompt 装配默认预算 | 100 000 token |
| 压缩触发 | 128 000 token |
| 安全余量比例 | 0.10 |
| 压缩后输入占比 | 0.85 |
| CJK 字符估算 | 3 token/字 |
| 非 CJK 估算 | 3 字符/token |
| `SMITH.md` 单文件上限 | 50 000 字符 |
| Prompt 前缀缓存条目上限 | 128 |

### 工具

| 参数 | 值 |
|---|---|
| 输出截断行数 | 2000 |
| 输出截断字节 | 50 KB |
| 权限等级 | read / write / execute / destructive |
| 审批策略 | never / policy / always |
| 副作用 | none / write / external / destructive |
| 并发 | safe / serial |
| 执行环境 | host / sandbox / either |

### 管线

| 参数 | 值 |
|---|---|
| 通用门禁最大重试 | 3 |
| 门禁 LLM 输入上限 | 2000 字符（两端各留一半） |
| `FailureLoopGuard.max_same` | 2 |
| `FailureLoopGuard.max_attempts` | 3 |

---

## 8. 这一层的设计取舍

**① 所有边界都是显式常量，不是魔法数字散落各处。** `budget.py` 一个文件放全部 ReAct 预算，改一个值不用翻 1386 行。

**② 每一条防御都对应一个复现过的 bug。** 对话裁剪的三段注释、流式回退的判据、草稿撤回的四种原因——它们看起来是过度设计，直到你读到注释里的实测数字。

**③ 弱类型换扩展性，但在边界上强校验。** 内容层返回 dict 就行（`coerce_gate_result` 适配），但适配函数会把每一种形状都验一遍。

**④ 确定性优先。** 假最终回答用正则不用模型，门禁先跑启发式再跑 LLM，路由纯词法。模型只在确定性方法真的做不了的地方出现。

**⑤ 该省的地方省。** 懒加载工具 schema、稳定前缀缓存、门禁走便宜模型路由、工具输出截断落盘——四个不同的省钱手段，都不牺牲正确性。

---

## 9. 接下来

| 想深入 | 读 |
|---|---|
| `ToolGuard` 那 1365 行到底在拦什么 | [06 · 安全与安全边界](./06-安全与安全边界.md) |
| 记忆视图怎么被编译出来 | [05 · 记忆系统](./05-记忆系统.md) |
| provider 适配器怎么产出这些事件 | [07 · LLM 集成](./07-LLM-集成.md) |
| 三条管线的门禁具体检查什么 | [08 · Agents 内容层](./08-Agents-内容层.md) |
