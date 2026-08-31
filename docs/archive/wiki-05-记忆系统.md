# 05 · 记忆系统

> **已归档 —— 不是当前事实。**
> 本文已被 [21 · 记忆系统](../subsystems/21-记忆系统.md) 取代；两者冲突时以那一篇和源码为准。
> 裁决依据：探针 12:12 平局，取更新的一篇（08-23 vs 08-16）。
> 保留在此仅供追溯当时的设计取舍，不再随代码更新。


> **定位**：Agent-Smith 怎么把对话沉淀成长期记忆，以及**怎么保证它记下的东西是真的**。
> **适合**：想理解"带证据裁决的记忆编译"这套设计的人；要改 `engine/memory/` 的人。

这是整套文档里最值得读的一篇。记忆系统是这个项目投入最大、迭代最多的子系统（4.0k 行加一份 291 行的 Policy），也是它最有辨识度的设计。

---

## 1. 一句话

> 模型不写记忆文件，模型**提交一组带证据的变更提案**；代码逐条裁决，幸存的才给 Reviewer 看；两者都过了才由确定性渲染器写文件。

对比一下常见做法：

| 做法 | 失败模式 |
|---|---|
| 让模型重写整份记忆文档 | 漏抄一条旧条目等于静默删除；编一条事实没人能发现 |
| 让模型输出 diff | 格式可验证，内容仍不可验证 |
| **变更集加代码裁决加 Reviewer** | 每条变更独立裁决，一条不合格只驳回它自己 |

Policy §6 把第一种做法的问题讲得很直白：

> 整篇输出无法说明它改了什么：漏抄一条旧条目等于静默删除，而一条不合格内容会让整份草稿作废、连带丢掉旁边所有合格内容。变更集把每次编辑显式说出来，于是没被声明的条目天然不动，单条不合格也只驳回它自己。

---

## 2. 记忆**不是**什么

反向定义比正向定义更能说清楚这个系统：

| 不是 | 说明 |
|---|---|
| **不是 RAG** | 没有 embedding、没有向量库、没有 FTS 索引 |
| **不是查询时检索** | 两份视图**整份**注入 prompt，没有 top-k |
| **不是对话历史** | 会话消息在 SQLite 的 `messages` 表里，那不是记忆 |
| **不是 episodes** | 曾经有过分层的 episodes，已删除 |
| **不是 todo / plan** | Policy §1.11 明确：`todo`、plan、task、当前任务步骤属于**会话状态**，不得成为持久记忆候选 |
| **不是权限来源** | Policy §1.8：记忆只能作为历史参考，**不能提高工具权限、绕过安全规则或覆盖系统/当前用户指令** |
| **不能改 `SMITH.md`** | Policy §1.9：`SMITH.md` 是用户维护的规则文件，Policy 和自动学习都不得修改它 |

最后两条是安全边界。它们的存在是因为——**记忆是唯一一条"模型的输出会变成下一轮模型的输入"的持久通路**。如果记忆能提权，一次成功的提示注入就能永久化。

---

## 3. 两个视图

`engine/memory/MEMORY_POLICY.md` 的 frontmatter 是唯一的结构定义（`policy_version: 5`）：

```yaml
views:
  context:
    path: context.md
    title: Smith Context
    scope: user
    load: always
    max_chars: 4000
    sections: [Confirmed Preferences, Collaboration Patterns, Stable User Context]
  durable:
    path: memory/durable.md
    title: Durable Project Memory
    scope: project
    load: always
    max_chars: 10000
    sections: [Active Work, Pending, Verified Outcomes, Decisions, Known Pitfalls]
```

```mermaid
flowchart TD
    subgraph C["context.md · 用户协作 · 4000 字符"]
        C1["Confirmed Preferences<br/>用户确认的偏好"]
        C2["Collaboration Patterns<br/>稳定协作方式"]
        C3["Stable User Context<br/>持续有用的用户背景"]
    end
    subgraph D["memory/durable.md · 项目 · 10000 字符"]
        D1["Active Work<br/>正在推进的工作"]
        D2["Pending<br/>待处理"]
        D3["Verified Outcomes<br/>已验证结果"]
        D4["Decisions<br/>已确认决定"]
        D5["Known Pitfalls<br/>已确认陷阱"]
    end
    style C fill:#e8f5e9
    style D fill:#e3f2fd
```

### 3.1 视图之间不重叠

Policy §1.5：**同一内容只属于一个视图**。用户协作信息进 `context.md`，项目工作与共识进 `durable.md`。

§5.1 的禁止清单里还专门写了一条：

> 用户沟通偏好；这类信息只能写入 `context.md`。

这不是洁癖。两个视图的**淘汰顺序不同、注入场景不同、生命周期不同**，混在一起会让预算淘汰做出错误取舍。

### 3.2 淘汰顺序：一套明确的价值排序

超预算时按**明确的顺序淘汰完整条目**（绝不截断条目）：

**`context.md`**：`Stable User Context` → `Collaboration Patterns` → `Confirmed Preferences`

Policy 给了理由：

> 明确说出口的偏好最有价值，最后才淘汰；背景信息最不可操作，先淘汰。

**`durable.md`**：`Active Work` → `Pending` → `Verified Outcomes` → `Decisions` → `Known Pitfalls`

这个顺序初看反直觉——为什么先淘汰"正在推进的工作"？因为**`Active Work` 是最容易重新获得的**：当前任务的状态在对话里就有。而 `Known Pitfalls`（已确认的陷阱）是最贵的知识，它来自一次真实的踩坑，丢了就要再踩一次。

**淘汰的单位是完整条目，同一章节按文档顺序从前往后。** 不截断，因为半条记忆比没有记忆更危险。

### 3.3 `durable.md` 没有时间窗

Policy §5 有一句关键澄清：

> `durable.md` 是 Smith 唯一的长期项目记忆。它**没有时间窗口**，也不会因为一段时间没有新事件而被清空；每次编译都是「现有 durable + 本次新增证据」的增量合并。

遗忘只有三个来源：

1. 用户要求忘记
2. 被更新的条目取代
3. 为守住字符预算按价值顺序淘汰

**没有"过期"这一项。** 这与 `recent.jsonl` 的 7 天保留窗形成对比——保留窗只作用在**原始事件日志**上，不作用在编译产物上。

---

## 4. 数据流全景

```mermaid
flowchart TD
    T["一轮对话结束"] --> S["store.py<br/>save_conversation_memory()"]
    S --> R[("recent.jsonl<br/>候选证据事件日志")]

    R -->|"每 10 轮或空闲 tick"| CD["compile.py<br/>compile_context(view=durable)"]
    R -->|"独立游标"| CC["compile.py<br/>compile_context(view=context)"]

    CD --> P1["按 .compile_offset 读增量<br/>装入 24k prompt 预算"]
    CC --> P2["按 .compile_offset_context 读增量"]

    P1 --> G["Compiler LLM<br/>输出 JSON 变更集"]
    P2 --> G
    G --> A["_guards.adjudicate()<br/>三道确定性裁决"]
    A -->|"逐条驳回"| REJ["RejectedChange<br/>写入 memory_history"]
    A -->|"幸存变更"| RV["_review.py<br/>Reviewer LLM"]
    RV -->|"hard_fail"| RETRY["重试，最多 3 轮"]
    RETRY --> G
    RV -->|"pass"| AP["_changeset.apply_changes()<br/>确定性应用"]
    AP --> EV["evict_to_budget()<br/>按价值顺序淘汰"]
    EV --> W["_files.atomic_write_text()<br/>路径检查 结构检查 安全扫描 备份 原子替换"]
    W --> SNAP["_snapshot.snapshot_views()<br/>git 提交两个视图"]
    W --> H[("memory_history.jsonl<br/>脱敏审计")]

    R -->|"每 50 次"| DR["dream.py<br/>run_dream()"]
    DR --> SAN["清洗所有记忆文件<br/>密钥与注入"]
    DR --> CL["回收 recent.jsonl 过期前缀<br/>只到两个游标里更靠后的那个"]
    CL --> J[".dream_cleanup.json<br/>先写日志再改游标"]

    style A fill:#ffe0e0
    style W fill:#e8f5e9
```

---

## 5. 证据日志：`recent.jsonl`

### 5.1 谁往里写

两条路径：

**① 自动**：每轮对话结束由 `store.py:save_conversation_memory()` 写一条 `work` 或 `partial_work` 事件。

**② 手工**：模型调 `memory_ops.add`。但注意 Policy §1.1 的措辞：

> `memory_ops.add` **不是"直接写记忆"**。它只能写入 `recent.jsonl` 的候选证据。

必须同时提供四个字段：

| 字段 | 取值 |
|---|---|
| `kind` | `preference` / `correction` / `decision` / `remember` / `forget` / `verified_fact` / `procedure` / `pitfall` |
| `scope` | `user` / `project` |
| `evidence_type` | `user_explicit` / `tool_result` / `test_result` / `source_document` |
| `content` 加 `evidence` | 受安全扫描 |

而 `plan` / `task` / `todo` / `task_step` **一律拒绝**。

> 写入成功只表示"候选证据已记录"；仍需 Compiler、Reviewer 和结构/安全检查后才可能进入正式 Markdown。

### 5.2 `kind` 的强度次序

`kind` 不只是标签，它决定这条证据**能支撑什么样的断言**：

```mermaid
flowchart TD
    subgraph 最强["能推翻结论"]
        K1["forget"]
        K2["correction"]
    end
    subgraph 中["能建立结论"]
        K3["verified_fact"]
        K4["decision"]
        K5["preference"]
        K6["procedure"]
        K7["pitfall"]
        K8["remember"]
    end
    subgraph 最弱["只能进 Active Work"]
        K9["work（助手自述）"]
        K10["partial_work"]
    end
    最强 --> 中 --> 最弱
    style 最强 fill:#ffcdd2
    style 最弱 fill:#f5f5f5
```

`_guards.py` 里的注释解释了为什么 `work` 最弱：

> 一个自动 `work` 事件的 summary 是**助手自己对做了什么的陈述**，不是工具或测试结果；`partial_work` 名字里就写着。两者都不能建立一个**已验证的**结果。

而且**没有 `kind` 的历史事件按 `work` 处理**——取最弱读法，失败方向安全。

### 5.3 证据优先级

Policy §2 定了一条冲突解决链：

1. 用户当前明确的忘记或纠正
2. 用户明确表达的偏好、决定或"请记住"
3. 文件、测试、工具结果或权威资料验证过的事实
4. 多次独立出现的稳定行为模式
5. 模型对单次对话的推断

> 低优先级证据不得覆盖高优先级证据。**无法判断哪条正确时，保留旧正式记忆，不写入新结论。**

最后一句是 fail-closed 的又一处体现：**不确定就不写**。

### 5.4 写入侧的约束

`store.py`：

| 常量 | 值 | 作用 |
|---|---|---|
| `_COMPILE_INTERVAL` | 10 | 每 10 轮触发一次编译 |
| `_MAX_EVENT_VALUE_CHARS` | 16 000 | 单个事件字段上限 |
| `_MAX_LEARNING_SIGNALS` | 16 | 一轮最多带 16 个学习信号 |
| `_RETRY_COOLDOWN_SECONDS` | 600 | 失败后的重试冷却 |

`_bounded_event_value()` 保证一条超长的工具输出不会把证据日志撑爆——同样是"截断但说明"的做法。

---

## 6. 编译：变更集契约

### 6.1 Compiler 只输出 JSON

Policy §6 的合同：

```json
{
  "nothing_to_record": false,
  "changes": [
    {"op": "add", "view": "durable", "section": "Decisions",
     "content": "- **{主题}**: 决定 {内容}；适用范围：{范围}。",
     "evidence": {"ref": "{recent.jsonl 中该条事件的 timestamp}",
                  "quote": "{该条事件文本中的原文片段}"}},
    {"op": "remove", "view": "durable", "section": "Active Work",
     "target": "**{主题}**", "reason": "{已完成 / 已被取代 / 用户要求忘记}",
     "evidence": {"ref": "...", "quote": "..."}},
    {"op": "replace", "view": "durable", "section": "Active Work",
     "target": "**{主题}**", "content": "- **{主题}** — {更新后的整条内容}",
     "evidence": {"ref": "...", "quote": "..."}}
  ]
}
```

七条硬性要求，每条都有理由：

| 要求 | 理由 |
|---|---|
| 只输出 JSON，不输出 Markdown 或解释 | 渲染由确定性代码做 |
| 用 `**{主题}**` 定位，不用行号 | 行号会因为别的变更而失效 |
| `add` / `replace` 的 `content` 必须是一整条符合模板的 bullet | 半条内容无法校验 |
| **`replace` 不得更改主题键** | 改名要拆成 `remove` 加 `add`，否则旧键对后续变更不可达 |
| 每条变更都要给 `evidence`，`ref` 真实存在、`quote` 是原文片段 | 第一道守卫的前提 |
| 没内容时输出 `nothing_to_record: true` | **"交白卷是合法且正确的结果，不要为了有产出而硬凑"** |
| 不提与本批证据无关的变更 | 证据不足就不提 |

第六条值得单独强调。大多数 prompt 会隐含地压模型"产出点什么"，而这里**显式地把空输出定义为正确结果**。没有这句话，模型会为了满足格式而编造记忆。

### 6.2 `_changeset.py` 的解析与应用

| 函数 | 作用 |
|---|---|
| `parse_changeset()` | JSON 转 `list[MemoryChange]` |
| `topic_key()` | 从 bullet 里抽 `**主题**`（正则 `^\s*-\s*\*\*(.+?)\*\*`） |
| `parse_document()` | 现有 Markdown 转 `{section: [bullets]}` |
| `apply_changes()` | 把变更应用到分组结构上 |
| `evict_to_budget()` | 按 Policy 顺序淘汰完整条目 |
| `render_document()` | 分组结构转最终 Markdown |
| `render_changeset()` | 变更集转成给 Reviewer 看的可读形式 |

`_find_indices()` 处理一个边界：**同一个主题可能出现多次**（历史遗留），返回全部索引而不是第一个。

---

## 7. 三道守卫

`engine/memory/_guards.py`（283 行）是整个系统的核心。它的模块 docstring 是设计意图的最好陈述：

> 三个模型不该被信任来评判自己输出的问题，而代码不需要第二意见就能判定：
> 1. **溯源**：被引用的证据存在吗？新内容留在它之内吗？
> 2. **保留**：这条 bullet 到底可不可以被删？
> 3. **落位**：这个章节允许承载这种强度的断言吗？
>
> 这些规则过去只约束确定性 fallback 渲染器，也就是**代码写的路径被要求得比 LLM 写的路径更严**。现在它们对每一条变更生效，在 reviewer 之前跑，所以一个通不过的变更集**不花 reviewer 的钱**，并且带着一个机器可检的理由而不是散文回到生成器。

最后一句还划了边界：

> 守卫刻意只判断有**可证伪锚点**的东西。"这个用户偏好简短回答"没有任何代码量能校验，Policy §7 把这一类交给 reviewer。

### 7.1 裁决是逐条的

```python
def adjudicate(changes, *, view, evidence, grouped):
    """Rejections are per change, never per change set: a single unsupported
    edit must not discard the supported ones beside it."""
    for change in changes:
        cited = _cited_entries(change, evidence)
        reject = (_check_traceable(...) or _check_retention(...) or _check_placement(...))
```

三道守卫**短路求值**：第一道拒了就不跑后两道。

### 7.2 第一道 · 溯源

```mermaid
flowchart TD
    A["一条变更"] --> B{"有 evidence_ref<br/>和 evidence_quote"}
    B -->|"否"| R1["evidence_missing"]
    B -->|"是"| C{"quote 归一化后至少 8 字符"}
    C -->|"否"| R2["evidence_quote_too_short"]
    C -->|"是"| D{"ref 在本批证据索引里"}
    D -->|"否"| R3["evidence_ref_not_in_batch"]
    D -->|"是"| E{"quote 出现在该事件文本中"}
    E -->|"否"| R4["evidence_quote_not_in_event"]
    E -->|"是"| F{"content 里每个可证伪锚点<br/>都能在证据池里找到"}
    F -->|"否"| R5["content_anchor_not_in_evidence"]
    F -->|"是"| PASS["通过第一道"]

    style R1 fill:#ffcdd2
    style R2 fill:#ffcdd2
    style R3 fill:#ffcdd2
    style R4 fill:#ffcdd2
    style R5 fill:#ffcdd2
```

**五个精心设计的细节：**

**① 证据索引从 prompt 文本建，不从 `recent.jsonl` 建。**

```python
"""Built from the prompt text rather than from ``recent.jsonl`` on purpose:
the source is capped, so the log holds entries the model never saw, and a ref
validated against the log could be accepted without ever having been read."""
```

日志里有模型**从没看到过**的条目（因为 prompt 有 24k 预算上限）。拿日志校验 ref，模型就能引用一条它根本没读过的证据——不是伪造，但同样是无根据的。

**② `_MIN_QUOTE_CHARS = 8`。**

```python
# A citation has to demonstrate that the entry was read. A one-character quote
# matches almost any entry, which would let the anchor pool resolve to some other
# event in the batch than the one the claim is attached to -- not fabrication, but
# misattribution.
```

一个字符的 quote 能匹配几乎任何条目，于是锚点池会解析到**批次里的另一个事件**上。这不是伪造，是**错误归因**，更隐蔽。

**③ 可证伪锚点的四个正则。**

| 模式 | 匹配 |
|---|---|
| `https?://\S+` | URL |
| 反引号包裹的内容 | `` `foo` `` |
| 带扩展名的路径（30 种扩展名，可带 `:行号`） | `loader.py:42` |
| `\d{6,}` | 6 位以上数字 |

**④ 路径正则前面那个 `\b` 是承重的，不是装饰。**

```python
# The leading \b on the path pattern is load-bearing, not decoration.  CJK
# characters are word characters, so without it `修复了loader.py` matches as one
# anchor and then fails to be found in evidence that says `loader.py` -- a
# spurious rejection.
```

CJK 字符在正则里算 word character，所以没有 `\b` 时 `修复了loader.py` 会被整体匹配成一个锚点，然后在只写了 `loader.py` 的证据里找不到——**误拒**。加上 `\b` 之后，粘着中文的路径干脆不被检查，**宁可放过也不误拒**。

这一条特别值得学：**一个安全检查在误报方向上的失败，代价可能比漏报更高**——因为误报会让整个机制被绕过或关掉。

**⑤ `replace` 可以沿用旧锚点。**

```python
if change.op == "replace":
    previous = accepted.get((change.section, change.target))
    if previous:
        pool.append(_normalize(previous))
```

被改写的那条已经通过过一次审核，所以只有**新增**的内容需要新证据。

### 7.3 第二道 · 保留

```mermaid
flowchart TD
    A["一条变更"] --> B{"op 是 add"}
    B -->|"是"| PASS["跳过本道"]
    B -->|"否"| C{"section 属于结论类"}
    C -->|"是"| D{"引用的证据里<br/>有 forget 或 correction"}
    D -->|"否"| R1["conclusion_changed_without_forget_or_correction"]
    D -->|"是"| PASS
    C -->|"否，流水类"| E{"有 reason<br/>或有 forget/correction"}
    E -->|"否"| R2["deletion_without_reason"]
    E -->|"是"| PASS

    style R1 fill:#ffcdd2
    style R2 fill:#ffcdd2
```

**结论类章节**（六个）：

```python
_CONCLUSION_SECTIONS = {
    "Verified Outcomes", "Decisions", "Known Pitfalls",                        # durable
    "Confirmed Preferences", "Collaboration Patterns", "Stable User Context",  # context
}
```

理由：

> 一个已定的结论不会因为**又干了一些活**而失效；抹掉它需要用户忘记它或纠正它。

**关键修复：`replace` 也算。**

```python
# `replace` counts, not just `remove`.  A replace keeps the topic key but may
# rewrite the entire body, so it erases a conclusion just as thoroughly -- and
# the new body is prose, which guard 1 cannot check for anchors it does not
# contain.  Leaving replace out made it the cheap way to invert a decision on
# the strength of routine work evidence.
```

这是一个真实的绕过漏洞：只管 `remove` 不管 `replace`，模型就能**保留主题键、改写整段正文**来推翻一条决定。而且新正文是散文，第一道守卫查不出"它没写"的锚点。

对应提交：`6b59de3 fix(memory): close the replace bypass in the retention guard`。

### 7.4 第三道 · 落位

**只作用于 `durable.md`**——`context.md` 的三节之间没有强度次序。

```mermaid
flowchart TD
    A["一条变更"] --> B{"view 是 durable<br/>且 op 不是 remove"}
    B -->|"否"| PASS["跳过本道"]
    B -->|"是"| C{"证据里有 partial_work<br/>且 section 不是 Active Work"}
    C -->|"是"| R1["partial_work_outside_active_work"]
    C -->|"否"| D{"section 是 Verified Outcomes"}
    D -->|"否"| PASS
    D -->|"是"| E{"证据全是 work 或 partial_work"}
    E -->|"是"| R2["unverified_evidence_in_verified_outcomes"]
    E -->|"否"| F{"正文里有「证据：」或 evidence 字段"}
    F -->|"否"| R3["verified_outcome_without_evidence_field"]
    F -->|"是"| PASS

    style R1 fill:#ffcdd2
    style R2 fill:#ffcdd2
    style R3 fill:#ffcdd2
```

第三个检查的理由写在常量旁边：

> `Verified Outcomes` 的模板带一个显式的证据字段；一个**没有陈述证据的已验证结果**，恰恰就是 Policy §5.1 禁止的那种未经验证的"已修复"断言。

`_EVIDENCE_FIELD_MARKERS = ("证据", "evidence")` 两种拼写都接受——模板是中文的，但没什么能阻止用户的项目记忆用英文写。

### 7.5 十个拒绝码

| 拒绝码 | 守卫 | 含义 |
|---|---|---|
| `evidence_missing` | 1 | 没给 ref 或 quote |
| `evidence_quote_too_short` | 1 | quote 少于 8 字符 |
| `evidence_ref_not_in_batch` | 1 | ref 不在本批（模型没读过的证据） |
| `evidence_quote_not_in_event` | 1 | quote 不是该事件的原文 |
| `content_anchor_not_in_evidence` | 1 | 内容里的 URL、路径、数字在证据里找不到 |
| `conclusion_changed_without_forget_or_correction` | 2 | 想用普通工作证据推翻结论 |
| `deletion_without_reason` | 2 | 删流水条目但没给理由 |
| `partial_work_outside_active_work` | 3 | 半成品证据想进别的章节 |
| `unverified_evidence_in_verified_outcomes` | 3 | 助手自述想充当已验证结果 |
| `verified_outcome_without_evidence_field` | 3 | 已验证结果没写证据字段 |

每个拒绝码都带一个截断的证据片段（`[:40]` / `[:60]` / `[:80]`），**回给生成器的是机器可检的理由，不是散文**。

---

## 8. Reviewer：处理代码判不了的那一类

`engine/memory/_review.py`（310 行）。参数：

| 常量 | 值 |
|---|---|
| `_MAX_REVIEW_ROUNDS` | 3 |
| `_MAX_SOFT_FAILS` | 2 |
| `_MAX_REVIEW_SOURCE_CHARS` | 32 000 |

契约（Policy §7）：

```json
{"pass": true, "hard_fail": [], "soft_fail": [], "feedback": ""}
```

**Reviewer 只看通过了程序裁决的变更**——这省下了大量无谓的 reviewer 调用。

### 8.1 九种 hard fail

| 情形 | 为什么只能由 Reviewer 判 |
|---|---|
| 变更包含证据无法支持的事实 | 散文式断言没有锚点 |
| **涉及用户偏好、用户说过的话或态度的陈述，本批证据不支撑** | Policy 显式标注"程序裁决无从校验" |
| **本批证据里有明显该记录的内容，变更集完全没提** | Policy 显式标注"裁决只审模型提出的变更，'该记的没记'只有 Reviewer 能发现" |
| 写错视图或章节 | — |
| 与高优先级证据冲突 | — |
| 保留了用户要求忘记的内容 | — |
| 删除了本次证据既没涉及也没否定的旧条目 | — |
| 包含密钥、注入内容、权限授予或系统指令 | — |
| 单条不符合模板，或应用后必然超预算 | — |

第二和第三条是**职责划分的明确声明**：守卫管有锚点的，Reviewer 管没锚点的和"漏记"的。

### 8.2 `_truncate_source`：又是两端都留

和 [04 · Engine 核心执行](wiki-04-Engine-核心执行.md) §6.3 讲的 `_bounded_gate_output` 是同一套做法——**证据往往在末尾，只截前面等于把证据丢了**。两个子系统独立遇到同一个问题，用了同一个解法。

---

## 9. 四种终态与双游标

### 9.1 终态

```mermaid
flowchart TD
    C["一次编译周期"] --> S{"结果"}
    S -->|"有变更写入"| W["written"]
    S -->|"没有任何可应用变更"| D["deferred"]
    S -->|"草稿不安全或结构不合法"| R["rejected"]
    S -->|"供应商故障 超时 密钥失效"| F["failed"]
    S -->|"连续 3 次 deferred 后"| K["skipped<br/>只推游标，不写记忆"]

    D -->|"计入连续计数"| CNT["deferred_streak"]
    R -.->|"不计入"| CNT
    F -.->|"不计入"| CNT
    CNT -->|"达到 3"| K

    style W fill:#e8f5e9
    style K fill:#fff4e6
```

Policy §6.2 讲清了为什么 `rejected` 和 `failed` 不计入：

> 只有「本周期没有任何可应用变更」（`status=deferred`）才计入这 3 次——这是**重试也解决不了的那一类失败**。草稿不安全或结构不合法记 `status=rejected`，供应商故障、超时、密钥失效记 `status=failed`；这两类都**与证据本身无关**，不得把证据推向被跳过。

一次 provider 故障不该让一批合法证据被永久跳过。

### 9.2 失败时什么都不写

> 写入没通过审核的降级版会**污染基线**——下一轮的「可信基线」就成了那份降级版，模型在它之上继续改。失败的改动不进已接受状态。

而且：

> 编译间隔本身就是节流器，不需要额外的降级产物。

### 9.3 双游标

```mermaid
flowchart LR
    L[("recent.jsonl")] --> A["durable 游标<br/>.compile_offset"]
    L --> B["context 游标<br/>.compile_offset_context"]
    A --> C["durable.md"]
    B --> D["context.md"]
    A -.-> R["Dream 回收区间<br/>到两个游标里更靠后的那个"]
    B -.-> R
```

Policy §6.2：

> 每个视图各有自己的消费游标，因为两个视图**消费日志的速度不同**。offset 只能推进到**本次真的送进 prompt 的那一条**为止：证据按前缀装入 prompt 直到预算用尽，装不下的留给下一周期。用一个共享游标、或把游标推到日志末尾，都会让**没被那个视图读过的证据进入 Dream 的回收范围**。

两个错误做法，同一个后果：**静默丢证据**。

对应提交：`4fe329c fix(memory): give each view its own log cursor, and separate deferred from rejected`。

### 9.4 编译参数

| 常量 | 值 | 说明 |
|---|---|---|
| `MAX_DURABLE_CHARS` | 10 000 | 来自 Policy frontmatter |
| `MAX_DURABLE_SOURCE_CHARS` | 24 000 | 送进 prompt 的证据上限 |
| `_DURABLE_REVIEW_TIMEOUT_SECONDS` | 300 | 审核超时 |
| `_MAX_DEFERRED_STREAK` | 3 | 连续 deferred 上限 |
| `_COMPILE_INTERVAL` | 10 | 每 10 轮触发 |

---

### 9.5 维护调度：`maintenance.py`

前面九节讲的是"编译一次会发生什么"，这一节讲**什么时候编译**。`engine/memory/maintenance.py`（483 行）拥有这块运行时策略，模块 docstring 划清了它和执行层的关系：

> This module **owns the runtime-facing policy** for compilation, periodic candidate and Dream hygiene while **accepting the required LLM clients from the execution composition root**.

策略在记忆模块自己手里，但 LLM 客户端由执行层注入——这样记忆模块不需要知道客户端怎么构造、谁负责关闭。

### 9.5.1 退避判据：内容拒绝 vs 传输失败

这是这个文件里最值得记的一条判断：

```python
# Review/content rejections deserve a fresh attempt soon; transport/provider
# failures (timeouts, unreachable providers, disk errors) back off so a
# due-but-failing lane cannot hammer the LLM every turn.
_REVIEW_REJECTION_MARKERS = (
    "did not pass review",
    "contains sensitive information",
    "instruction-injection",
    "exceeded character budget",
    "LLM returned insufficient output",
    "requires a reviewer",
)
```

两类失败的**处理方向相反**：

| 失败类型 | 例子 | 退避吗 | 为什么 |
|---|---|---|---|
| **内容/评审拒绝** | 没通过评审、含敏感信息、指令注入、超字符预算、输出不足 | **不退避** | 下一轮的证据不一样，很可能就过了；冷却只会白白拖慢记忆积累 |
| **传输/provider 失败** | 超时、provider 不可达、磁盘错误 | **退避** | 短期内不会自愈；不冷却的话，一条到期但持续失败的通道会**每一轮都去锤 LLM** |

判据有两条路径：

```python
if exc is not None:
    if isinstance(exc, (MemoryCompilationError, MemoryPolicyError)):
        return False          # 这两类是内容问题，不退避
    return True               # 其他异常一律退避
if error_text is not None:
    return not any(marker in error_text for marker in _REVIEW_REJECTION_MARKERS)
return True                   # 什么都不知道时，保守退避
```

**有异常对象时按类型判断**（可靠），**只有错误文本时按标记串匹配**（次选）。两条路径的默认方向都是"退避"——不确定时冷却是安全的，最坏只是慢一点；反过来默认不退避，一个持续失败的通道会把 LLM 配额烧光。

> 这是本套文档里少见的**允许按文本匹配**的地方。它之所以可以接受，是因为这些文本是**本模块自己产生**的错误消息（不是 provider 返回的），而且判错的代价只是"多等一会儿"或"多试一次"，不涉及安全或数据正确性。对比 [07 · LLM 集成](../subsystems/25-LLM集成.md) §15.1 ⑤ 那条"绝不看自由文本"——那里判错会导致凭据泄漏或重试风暴，代价完全不同。

### 9.5.2 三条触发路径

```mermaid
flowchart TD
    A["record_turn()<br/>每轮对话结束"] --> B{"defer_maintenance"}
    B -->|"false"| C["立即执行<br/>_run_compilation_unlocked"]
    B -->|"true"| D["调度到后台<br/>_schedule_compilation"]
    E["run_compile()<br/>显式触发"] --> C
    F["run_idle_maintenance()<br/>空闲重试"] --> G{"到期或<br/>上次留下 pending"}
    G -->|"是"| C
    G -->|"否"| H["跳过"]

    C --> I["_mark_completed<br/>清 .compile_pending"]
    D --> J[".compile_pending 保留<br/>直到后台任务完成"]

    style D fill:#fff3cd
```

| 入口 | 时机 | 特点 |
|---|---|---|
| `record_turn()` | 每轮对话结束 | 按阈值决定要不要真跑；失败只记 warning **返回 False**，不影响对话 |
| `run_compile()` / `run_dream()` | 用户显式触发 | 无条件跑一次 |
| `run_idle_maintenance()` | 空闲时 | **只重试到期或上次遗留 pending 的**，不做多余工作 |

`defer_maintenance` 这个开关的注释解释了它的用途：

> This timeout is for explicit/idle maintenance; **production turn finalization defers this work when the runtime owns shared LLM clients**.

生产环境里，一轮对话结束时运行时还握着共享的 LLM 客户端。此时同步跑编译会：占用那些客户端、拉长用户感知的响应时间、并且如果客户端随后被关闭还会中途失败。所以改成**调度到后台**，让轮次先干净地结束。

### 9.5.3 两个 pending 标记文件

```python
_COMPILE_PENDING_FILE = ".compile_pending"
_DREAM_PENDING_FILE = ".dream_pending"
MAINTENANCE_KINDS: tuple[str, ...] = ("compile", "dream")
```

标记文件回答一个问题：**上次该做的事做完了吗？** 它们在开始时写下、完成时删除，所以：

- 进程崩在编译中途 → 标记还在 → 下次 `run_idle_maintenance()` 会重试
- 正常完成 → 标记已删 → 空闲时不做无用功

用文件而不是内存标志，正是为了**跨进程重启存活**。这和 [10 · 可观测性与诊断](../subsystems/27-可观测性.md) §10 的崩溃恢复是同一类设计：把"我正在做某事"这个事实落盘，让下一个进程能接着处理。

`_MEMORY_MAINTENANCE_TIMEOUT_SECONDS = 900.0`（15 分钟）的注释说明了为什么这么长：

> **Three policy views may each consume a generator/reviewer round.**

每个视图都要走一轮生成 + 一轮评审，三个视图最多六次 LLM 调用。15 分钟是给这整串留的余量，不是给单次调用的。

### 9.5.4 锁与后台任务是类变量

```python
_locks: ClassVar[dict[Path, asyncio.Lock]] = {}
_background_tasks: ClassVar[dict[tuple[Path, str], asyncio.Task[None]]] = {}
```

两个字典都是 `ClassVar`——**所有 `MemoryMaintenanceService` 实例共享**。这是有意的：服务本身是 `frozen=True` 的数据类，可能被反复构造（每个请求一个），但**同一个记忆目录的维护必须串行**。

如果锁放在实例上，两个请求各自构造一个 service，各自拿到自己的锁，两个编译会同时跑——同一份 `recent.jsonl` 被读两遍、游标被推进两次、两份变更集互相覆盖。

`_background_tasks` 的键是 `(memory_dir, kind)` 二元组，所以编译和 Dream 可以并行，但同一种维护对同一个目录只有一个在跑。

---

## 10. Dream：记忆卫生

`engine/memory/dream.py`（418 行）。Policy §8 明确它的边界：

> Dream 是低频的记忆卫生作业，**不产生新知识，也不改写记忆内容**。

| 常量 | 值 |
|---|---|
| `DREAM_INTERVAL` | 50（次） |
| `EVIDENCE_RETENTION_DAYS` | 7（天） |

### 10.1 两件事

```mermaid
flowchart TD
    A["run_dream()"] --> R["_recover_dream_cleanup()<br/>先恢复上次未完成的日志"]
    R -->|"恢复失败"| STOP["记失败并返回"]
    R -->|"OK"| S["_sanitize_all_layers()<br/>在线程里跑，git 是阻塞子进程"]
    S --> C["_cleanup_log()<br/>回收过期前缀"]
    C --> H["append_memory_history(target=dream)"]
```

**清洗跑在线程里**，注释说明了原因：

> Off the event loop: sanitation snapshots through git, whose blocking subprocess calls would otherwise stall every request this process is serving.

### 10.2 回收的三条约束

Policy §8：

> - 只有**两个视图都已消费**、且超过保留窗口的 `recent.jsonl` 行才能回收：可回收区间到两个游标里**更靠后的那个**为止
> - 回收只能删除**连续的过期前缀**，不能跨过窗口内事件
> - 替换 `recent.jsonl` 前必须记录旧/新日志哈希与**两个** offset

### 10.3 Cleanup journal：为什么必须先写日志

删掉前缀会让后面每一行**位移**。没被重定基的游标会指过已经下移的证据，**把它静默跳过**。

```mermaid
sequenceDiagram
    participant D as Dream
    participant J as .dream_cleanup.json
    participant L as recent.jsonl
    participant C as 两个游标

    D->>J: ① 写日志（旧哈希 新哈希 两个 offset）
    D->>L: ② 原子替换日志文件
    D->>C: ③ 重定基两个游标
    D->>J: ④ 清除日志

    Note over D,C: 崩在任何一步之间，下次启动由<br/>_recover_dream_cleanup() 补完或安全放弃
```

`_load_dream_cleanup()` 的校验极其严格：

```python
if (isinstance(cleaned, bool)          # bool 是 int 的子类，必须先排除
    or not isinstance(cleaned, int) or cleaned <= 0
    or not isinstance(old_recent_hash, str) or len(old_recent_hash) != 64
    or not isinstance(new_recent_hash, str) or len(new_recent_hash) != 64
    or isinstance(compile_offset, bool) or not isinstance(compile_offset, int) or compile_offset < 0
    or isinstance(context_offset, bool) or not isinstance(context_offset, int) or context_offset < 0):
    return None, "Dream cleanup has invalid fields"
```

`isinstance(x, bool)` 的显式排除是必要的——Python 里 `True` 是 `int`，一个 `"cleaned": true` 会通过 `isinstance(cleaned, int)`。

还有两处向后兼容，都选择了**保守恢复**而不是拒绝：

```python
# A journal written before context.md had its own cursor has no
# ``context_offset``.  Recovering it as 0 makes context re-read the trimmed
# log from the start: redundant work, never lost evidence.
context_offset = payload.get("context_offset", 0)
```

```python
# A journal written before the memory views were merged also carries
# durable/dream/nudge offsets.  Those lanes no longer exist; ignore the
# extra keys rather than rejecting a journal that must still be recovered.
```

**多余的键忽略，缺失的键取最保守值**——因为这份日志代表一次未完成的破坏性操作，拒绝恢复它比多做点冗余工作危险得多。

而且读取走 `safe_file_in_dir()`，**不跟随不安全的软链**。

---

## 11. 写入与审计

### 11.1 写入的六道手续

Policy §9：

> 正式 Markdown 只有在变更集通过程序裁决与 Reviewer 之后才可写入，**没有例外**。写入必须执行路径检查、结构检查、字符预算、安全扫描、备份、原子替换，并在写入后留一份可回滚的版本快照。

```mermaid
flowchart LR
    A["变更集通过"] --> B["路径检查<br/>safe_file_in_dir"]
    B --> C["结构检查<br/>validate_rendered_view"]
    C --> D["字符预算<br/>evict_to_budget"]
    D --> E["安全扫描<br/>sanitize_memory_text"]
    E --> F["备份 .bak"]
    F --> G["原子替换<br/>atomic_write_text"]
    G --> H["git 快照<br/>snapshot_views"]
```

### 11.2 git 快照

`engine/memory/_snapshot.py`（107 行，`_TIMEOUT_SECONDS = 15.0`）：每次接受的写入后 git 提交两个视图。

为什么不是 `.bak`：`.bak` 只能回退一代，而记忆编译**每轮都可能发生**。一个坏写入之后再来两轮正常写入，`.bak` 里就只剩坏的了。

### 11.3 审计历史

`memory_history.jsonl`（`engine/memory/history.py`）：

```json
{
  "timestamp": "ISO-8601",
  "target": "context|durable|dream|{被清洗的文件名}",
  "policy_version": 5,
  "status": "written|unchanged|deferred|skipped|sanitized|rejected|failed",
  "old_hash": "...", "new_hash": "...",
  "review_rounds": 1, "error": null
}
```

Policy §9 的一句话解释了为什么被拒的变更也要记：

> 被驳回的变更与因预算被淘汰的条目必须一并记录（内容与原因），否则「哪条记忆没写进去、为什么」无从追查——**一条无声消失的记忆和一条从未产生的记忆无法区分**。

参数：

| 常量 | 值 |
|---|---|
| `_MAX_HISTORY_ENTRIES` | 500 |
| `_MAX_HISTORY_AGE_DAYS` | 90 |
| `_FAILURE_TAIL_BYTES` | 65 536 |
| `_FAILURE_ERROR_CHARS` | 160 |

`recent_failure_streak()` 只读日志的**尾部 64KB**——判断"最近连续失败了几次"不需要读整个文件。

**这份日志不进 prompt**：Policy §9 最后一句"该日志用于解释和排错，不作为 Smith 正常回答时的 Prompt 内容"。

---

## 12. 安全：`_files.py`

| 函数 | 作用 |
|---|---|
| `interprocess_file_lock()` / `async_interprocess_file_lock()` | 跨进程文件锁 |
| `contains_secret()` | 密钥检测 |
| `contains_injection()` | 提示注入检测 |
| `sanitize_memory_text()` | 清洗，返回 `(文本, 密钥数, 注入行数)` |
| `safe_file_in_dir()` | 路径必须在目录内且非软链 |
| `safe_markdown_files()` | 安全地枚举 Markdown 文件 |
| `atomic_write_text()` | 原子写 |
| `append_private_lines()` | 追加并保持 `0o600` |

`_PRIVATE_KEY_FENCE_BEGIN` / `_PRIVATE_KEY_FENCE_END` 两个正则专门处理 PEM 私钥块——一个私钥横跨多行，逐行扫描会留下中间的 base64。

`sanitize_memory_text()` **分开返回密钥数和注入行数**，因为这两类问题的严重性和处置方式不同，审计时需要区分。

---

### 12.1 十条密钥模式

`SECRET_PATTERNS` 覆盖十种凭据形状，`dream.py` / `store.py` / `memory_ops.py` 共用：

| 模式 | 匹配 |
|---|---|
| `(?<![a-zA-Z])sk-[A-Za-z0-9_\-]{20,}` | OpenAI 风格 key，**带左边界** |
| `AKIA[0-9A-Z]{16}` | AWS access key id |
| `AIza[0-9A-Za-z_\-]{35}` | Google API key |
| `(?:ghp\|gho\|ghu\|ghs\|ghr\|github_pat)_...` | GitHub 六种 token 前缀 |
| `xox[baprs]-...` | Slack token 五种类型 |
| `(?:sk\|pk)_(?:live\|test)_...` | Stripe 密钥与公钥、生产与测试 |
| `(?i)(?:api[_-]?key\|password\|secret\|token\|credential)["']?\s*[:=]\s*["']?\S{8,}` | **通用赋值形式** |
| `(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}` | Authorization 头 |
| `(?i)(?:postgres\|mysql\|mongodb\|redis)://\S+:\S+@` | 连接串里的内嵌口令 |
| `-----BEGIN ... PRIVATE KEY-----` | PEM 私钥围栏 |

第一条的 `(?<![a-zA-Z])` 负向后顾和 [10 · 可观测性与诊断](../subsystems/27-可观测性.md) §3.2 里 trace 脱敏的边界断言是同一个问题——`sk-` 会出现在 `task-scheduler`、`disk-usage`、`risk-scoring` 这类普通词里，没有边界就会把整个文件名当成密钥吃掉。

第七条最宽：任何 `xxx_key: <8个以上非空白字符>` 形式都算。它必然有假阳性（比如 `token: whatever`），但记忆是**长期留存并注入 prompt** 的，宁可多删。

### 12.2 六条注入模式与两处假阳性防护

```python
_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions"),
    re.compile(r"(?i)you\s+(?:are|must)\s+now\s+(?:a|an|the)\s+\w+"),
    re.compile(r"(?i)(?:^|\s)system\s*:\s*(?:you\b|your\b|ignore\b|forget\b|"
               r"disregard\b|act\s+as\b|from\s+now\s+on\b|new\s+\w+\s*:)"),
    re.compile(r"(?i)new\s+(?:system\s+)?(?:role|instruction|policy)"),
    re.compile(r"(?i)override\s+(?:your|the|all)\s+(?:instructions|rules|policy)"),
    re.compile(r"忽略(?:之前|上面|先前)(?:的)?(?:所有|全部)?(?:指令|指示|规则|提示)"),
]
```

两条模式带着注释，各挡一种假阳性：

**① 冠词是必需的。**

```python
# An article is required: "you are now a pirate" is a role change, while
# "you are now ready to deploy" is ordinary prose that memory must keep.
```

`you are now (a|an|the) \w+` 要求冠词。没有这个约束，`you are now ready to deploy`、`you are now on the main branch` 这类**完全正常的项目笔记**都会被当成角色劫持删掉——而这正是记忆最该保留的内容。

**② 裸的 `system:` 不算注入。**

```python
# A bare "system:" appears in ordinary notes ("system: darwin 25.5.0").
# Only treat it as injection when it introduces an instruction.
```

`system: darwin 25.5.0` 是环境记录。所以模式要求 `system:` 后面**跟着一个指令性的词**（`you` / `your` / `ignore` / `forget` / `disregard` / `act as` / `from now on` / `new xxx:`）才算。

最后一条中文模式的存在说明这套检测面向的是**实际会遇到的注入**，而不只是英文样本。

`contains_injection` 先做空白归一化：

```python
normalized = re.sub(r"\s+", " ", text).strip()
```

这挡住了用换行或多空格拆开关键词的规避手法——`ignore\n\n  previous   instructions` 归一化后就是标准形式。

### 12.3 三层清洗与"按实际触发的类别计数"

`sanitize_memory_text()` 返回 `(清洗后文本, 删掉的密钥行数, 删掉的注入行数)`，处理分三层：

```mermaid
flowchart TD
    A["逐行扫描"] --> B{"在私钥块内"}
    B -->|"是"| C["整行丢弃<br/>secrets_removed++<br/>遇到 END 退出块"]
    B -->|"否"| D{"是 BEGIN PRIVATE KEY"}
    D -->|"是"| E["进入私钥块<br/>secrets_removed++"]
    D -->|"否"| F{"contains_secret(line)"}
    F -->|"是"| G["丢弃<br/>secrets_removed++"]
    F -->|"否"| H{"contains_injection(line)"}
    H -->|"是"| I["丢弃<br/>injections_removed++"]
    H -->|"否"| J["保留"]
    J --> K["逐行扫完"]
    K --> L{"整体含注入<br/>但逐行没删到"}
    L -->|"是"| M["整段丢弃<br/>按注入计数"]
    L -->|"否"| N{"整体含密钥<br/>但逐行没删到"}
    N -->|"是"| O["整段丢弃<br/>按密钥计数"]
    N -->|"否"| P["返回保留的行"]

    style M fill:#ffcdd2
    style O fill:#ffcdd2
```

**① 逐行删除保留无关上下文。** docstring 说明了为什么不整段丢：

> Line-level removal **preserves unrelated user-authored context** while ensuring a known unsafe fragment cannot survive into the prompt layer.

一段笔记里混进一行密钥，删那一行就够了，其余内容仍然有价值。

**② 私钥要成块删除。**

> Multi-line secrets (e.g. PEM private keys) are dropped as a block: **only the `-----BEGIN ... KEY-----` line matches** the line-level secret scan, so the base64 body would otherwise survive.

PEM 私钥只有首尾两行有特征，中间几十行 base64 逐行看都是普通字符串。用一个 `in_private_key_block` 状态位，从 BEGIN 一直删到 END。

**③ 跨行匹配要整段丢，且类别要报对。**

```python
if contains_injection(text) and injections_removed == 0:
    return "", secrets_removed, injections_removed + 1
if contains_secret(text) and secrets_removed == 0:
    return "", secrets_removed + 1, injections_removed
```

有些模式**跨换行才匹配**（`password:\n  <value>`），逐行扫时任何一行都不命中。所以扫完之后再对整段文本查一次，命中就把整段丢掉。

注释记录了一个修过的 bug：

> Drop the whole text, and **count it under the category that actually fired** — reporting a leaked credential as an "instruction-injection" made `store.sanitize_event_value` write the **wrong redaction notice** into memory, sending a **reviewer looking for an attack when the event was a secret**.

原先这两个分支的计数写反了。后果不是数字不准，而是**记忆里留下了错误的脱敏说明**——评审员看到"这里删掉了一次注入攻击"，去追查根本不存在的攻击，而真实情况是有人不小心把凭据写进了对话。

这类 bug 很有代表性：代码行为（删掉了）是对的，只有**元数据**（为什么删）是错的，测试如果只断言"内容被删除"就发现不了。

---

## 13. 用户偏好学习

`engine/memory/user_learner.py`（189 行）是一条独立的轻量通路：

```python
_CONFIDENCE_THRESHOLD = 3
```

四个检测器：

| 函数 | 检测 |
|---|---|
| `_detect_language()` | 中文 / 日文 / 其它（用 Unicode 区间正则） |
| `_detect_verbosity()` | 偏好详细还是简短 |
| `_detect_tech_level()` | 是否用专家词汇（`_EXPERT_KEYWORDS`） |
| `_detect_code_style()` | 代码风格倾向 |

**阈值 3** 直接对应 Policy §4.1：

> 至少**三次独立观察**到的稳定工作习惯。

流程是 `observe()` 抽信号，写入候选，编译通过后 `acknowledge()`。至少一次投递：没确认就下次重来（见 [04 · Engine 核心执行](wiki-04-Engine-核心执行.md) §7.3）。

---

## 14. 参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| Policy 版本 | 5 | `MEMORY_POLICY.md` frontmatter |
| `context.md` 预算 | 4 000 字符 | 同上 |
| `durable.md` 预算 | 10 000 字符 | 同上 |
| 送进 prompt 的证据上限 | 24 000 字符 | `compile.py` |
| Reviewer 源文本上限 | 32 000 字符 | `_review.py` |
| 审核轮数上限 | 3 | `_review.py` |
| soft fail 上限 | 2 | `_review.py` |
| 审核超时 | 300 秒 | `compile.py` |
| 维护整体超时 | 900 秒 | `maintenance.py` |
| 编译触发间隔 | 10 轮 | `store.py` |
| Dream 触发间隔 | 50 次 | `dream.py` |
| 证据保留窗口 | 7 天 | `dream.py` |
| 连续 deferred 上限 | 3 | `compile.py` |
| 最短 quote | 8 字符 | `_guards.py` |
| 单事件字段上限 | 16 000 字符 | `store.py` |
| 一轮学习信号上限 | 16 | `store.py` |
| 失败重试冷却 | 600 秒 | `store.py` |
| 偏好置信阈值 | 3 次 | `user_learner.py` |
| 历史条目上限 / 保留天数 | 500 / 90 | `history.py` |
| git 快照超时 | 15 秒 | `_snapshot.py` |

---

### 14.1 测试锁住了什么

`engine/tests/memory/` **157 个测试**。分布本身说明了这个子系统的风险集中在哪：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_memory_pipeline.py` | 50 | 端到端编译流程 |
| **`test_memory_guards.py`** | **32** | 三道守卫 |
| `test_memory_maintenance.py` | 25 | 调度与退避 |
| `test_memory_policy.py` | 18 | 策略文档的结构约束 |
| `test_memory_changeset.py` | 14 | 变更集解析与应用 |
| `test_memory_snapshot.py` / `test_memory_files.py` | 各 9 | git 快照、密钥与注入检测 |

**守卫单独占 32 个**——因为这是唯一一处"判错就会污染长期记忆"的地方，而记忆是会被注入到未来每一次 prompt 里的。

### 14.1.1 变更集与守卫

| 测试 | 锁住 |
|---|---|
| **`structural_rejects_never_reach_the_applier`** | 结构性拒绝在到达应用器**之前**就被拦下 |
| **`malformed_payload_is_a_reject_not_a_crash`** | 畸形载荷是**拒绝**不是**崩溃** |
| `duplicate_add_is_rejected_not_duplicated` | 重复的 add 要被拒，不能写两遍 |
| **`placement_does_not_constrain_the_context_view`** | 落位守卫**只管 durable 视图**，不约束 context |
| `compile_durable_rejects_free_form_output_and_keeps_old_view` | 自由文本输出被拒，**旧视图保留** |
| `compile_durable_rejects_oversize_output_without_replacing_memory` | 超长输出被拒，**不替换现有记忆** |

第二个测试的命名把设计意图说得很直白：**畸形输入的正确响应是拒绝，不是异常**。守卫处理的是 LLM 生成的内容，而 LLM 什么都可能吐出来——每一种畸形都让进程崩溃是不可接受的。

后两个测试共享同一个词："keeps the old view" / "without replacing memory"。这对应 §9.2 那条"失败时什么都不写"：一份被拒绝的草稿绝不能成为下一轮的基线，否则记忆会一轮轮退化。

`placement_does_not_constrain_the_context_view` 守的是 §7.4 的边界——落位守卫规定 `work` 类证据不能建立 `Verified Outcomes` 条目，但这条只适用于 durable 视图；context 视图（用户协作偏好）没有这种分区，套用会误杀。**同一道守卫对两个视图的适用范围不同**，是很容易在重构时被抹平的细节。

### 14.1.2 游标与终态

| 测试 | 锁住 |
|---|---|
| **`the_cursor_only_advances_past_events_that_fitted_the_prompt`** | 游标只推进过**真正放进 prompt** 的事件 |
| **`three_rejected_cycles_skip_the_batch_without_writing_memory`** | 三次 deferred 后跳过批次，**仍然不写** |
| `save_conversation_memory_keeps_compile_counter_when_durable_output_is_rejected` | 被拒时**计数器不重置** |
| `generate_and_review_rejects_a_draft_that_never_passes_review` | 永远过不了评审的草稿最终被放弃 |

第一个是双游标机制的核心不变量（§9.3）。如果游标推过了没能进 prompt 的事件，那些证据就**永久丢失**了——它们既没被编译进视图，也不会再被读到。

第二个对应 §9.1 的四种终态：连续三次 `deferred`（没有可用证据）后跳过，但**只推游标不写内容**。测试名里的 "without writing memory" 是关键——跳过不等于写一份空的。

### 14.1.3 维护调度

| 测试 | 锁住 |
|---|---|
| **`deferred_memory_maintenance_does_not_block_turn_and_can_be_drained`** | 延后的维护**不阻塞对话**，且可被排空 |
| `deferred_memory_maintenance_uses_background_llm` | 用**后台路由**的 LLM，不占交互路由 |
| `shared_runtime_defers_heavy_memory_maintenance` | 运行时握着共享客户端时**必然延后**（§9.5.2） |
| `deferred_schedule_respects_dream_cooldown` | 延后调度仍受 Dream 冷却约束 |

第一个测试名里的两半缺一不可：**不阻塞**（用户体验）**且可排空**（数据不丢）。只做到前者会让维护无限期堆积，只做到后者就退化成同步执行。

`uses_background_llm` 对应 [07 · LLM 集成](../subsystems/25-LLM集成.md) §2.3 的三条路由——记忆编译走 background 档，它有更长的超时和更便宜的模型配置，因为没人在等它。

### 14.1.4 Dream 与安全

| 测试 | 锁住 |
|---|---|
| **`dream_recovers_cleanup_after_log_replacement_without_replaying_evidence`** | 日志被替换后能恢复清理，**且不重放证据** |
| **`dream_rejects_recent_evidence_symlink_outside_memory`** | 证据日志是指向记忆目录外的符号链接 → 拒绝 |
| `dream_sanitize_leaves_a_trace_and_a_snapshot` | 净化操作要留下 trace 和 git 快照 |
| `memory_policy_rejects_wrong_or_extra_markdown_sections` | 策略文档的章节结构被严格约束 |

第一个对应 §10.3 的 cleanup journal：日志文件被替换（比如手工编辑过）之后，Dream 要能从 journal 恢复清理进度，而**不能把已经处理过的证据再编译一遍**——那会让同一条事实在记忆里出现两次。

第二个是路径安全：`recent.jsonl` 如果是一个指向 `~/.ssh/` 的符号链接，Dream 的截断操作就会破坏那个文件。这和 [13 · Common 基础设施](../layers/40-Common.md) §2.6 的受管路径检查是同一类防护，只是这里防的是**读**而不是写。

最后一个守着 `MEMORY_POLICY.md` 本身——策略文档规定了两个视图的章节结构，编译产物必须严格符合。多一个章节、少一个章节、章节名不对，都要拒绝。这让 §7 的落位守卫有一个稳定的结构可以依赖。

---

## 15. 演进：删掉了什么

这个子系统的历史比它现在的样子更有信息量。

```mermaid
timeline
    title 记忆系统的四次重构
    section 四层时间分层时代
        recent / working / durable / episodes : 加 FTS 索引与向量检索
        暴露的问题 : 中文 FTS 实际失效 · memory_ops 写入失联 · 路径穿越漏洞 · 零管线测试
    section 收敛为两视图
        删除 episodes 与 embeddings : 只留 context.md 与 durable.md
        删除查询时检索 : 改为整份注入
    section 变更集化
        048b1b2 : Compiler 产出 change set 而非文档，视图获得 git 快照
        fa130ee : 三道守卫在 Reviewer 之前逐条裁决
    section 补洞
        4fe329c : 双游标，deferred 与 rejected 分离
        6b59de3 : 堵住 replace 绕过留存守卫的通路
        9142d06 : 关闭证据伪造洞与四个游标陷阱
```

三条**被否决**的早期设计，写在这里是为了避免有人照旧版改：

| 被否决的设计 | 否决理由 |
|---|---|
| `recent.md` 作为一个渲染视图 | 与证据日志职责重叠；渲染它没有消费方 |
| `episodes` 分层 | 需要检索才能用，而检索被否决了 |
| 失败时写降级产物 | 降级版会成为下一轮的可信基线 |

---

### 15.1 贯穿这个子系统的五条原则

记忆是整个项目里防御最密的部分——因为它的错误**会被写进未来每一次 prompt**。五条原则解释了为什么这里的机制比别处重。

**① 被拒绝的产物绝不能成为下一轮的基线。** 编译失败什么都不写（§9.2）、超长输出保留旧视图、自由文本输出保留旧视图。理由是记忆有**自我强化**特性：这一轮写进去的内容会成为下一轮编译的输入，一份降级的草稿会一轮轮把记忆稀释掉，且没有任何一步会报错。

**② 游标只能推过真正被消费的证据。** §9.3 的双游标、`cursor_only_advances_past_events_that_fitted_the_prompt` 这个测试。游标推过去而内容没进视图的事件，是**永久丢失**的——它既不在记忆里，也不会再被读到。宁可重复处理，不能跳过。

**③ 结论只能被显式的遗忘或更正抹掉。** 保留守卫（§7.3）不允许一次普通的编译删除既有结论。这挡住的是一类很隐蔽的失败：LLM 在证据不足的一轮里"总结"出一份更简短的记忆，把上周的重要结论悄悄删了。

**④ 元数据和内容一样要对。** §12.3 那个把密钥报成注入的 bug——删除行为是对的，只有"为什么删"错了，结果把评审员引向不存在的攻击。记忆里的每一条脱敏说明都会被人读，写错方向比不写更糟。

**⑤ 假阳性和假阴性的代价不对称，且不同层不一样。** 密钥检测宁可多删（§12.1 第七条模式很宽），因为漏一个凭据会长期留在 prompt 里；注入检测则要精确（§12.2 两处冠词/上下文约束），因为误删的是用户真正需要的项目笔记。**同一个文件里两套模式的松紧方向相反**，是刻意的。

这五条里，①③是记忆特有的（因为存在自我强化循环），②在 [10 · 可观测性与诊断](../subsystems/27-可观测性.md) §10.3 的增量摘要里也成立，④⑤在 [06 · 安全与安全边界](wiki-06-安全与安全边界.md) 有对应表述。

自我强化这一点值得多说一句，因为它解释了为什么记忆的防御强度看起来和它的代码量不成比例。别的子系统里，一次错误就是一次错误——工具调用失败了就是失败了，下一次调用不受影响。而记忆的输出是它自己下一轮的输入：一条被错误写入的结论会参与下一次编译，可能被"确认"、被扩写、被当成既有事实引用，几轮之后已经无法分辨它最初是怎么来的。这种放大效应意味着**入口处多花的每一份严格，都比事后任何补救便宜**。三道确定性守卫加一道 LLM 评审，四层把关只为了让一件事成立：写进去的每一句话，都能回溯到一条真实发生过的证据。

### 15.2 改记忆系统之前先问三个问题

**① 这个改动会不会让一次失败留下部分写入？** 记忆的写入必须是全有或全无。任何新增的写路径都要确认失败时旧视图完好——`compile_durable_rejects_oversize_output_without_replacing_memory` 这类测试就是这条的守卫。

**② 这个改动会不会推进游标？** 推游标等于宣告"这些证据已经被处理了"。只有在证据真的进入了某个视图之后才能推。加一条提前返回的分支时，尤其要检查它有没有绕过这个判断。

**③ 这个改动碰守卫了吗？** 三道守卫（§7）是记忆唯一的把关处，它们之后就是直接写盘。改动守卫要跑满 `test_memory_guards.py` 的 32 个测试，并且想清楚新逻辑对**两个视图**分别意味着什么——`placement_does_not_constrain_the_context_view` 提醒的正是这一点。

还有一条不算问题、但值得在动手前先确认的事：**这件事真的需要 LLM 吗？** 三道守卫全是确定性代码，评审员只处理"代码判不了的那一类"（§8）。这个分工是刻意的——能用规则表达的约束就不要交给模型，因为模型的判断不可复现、不可测试、且每次都要花钱和时间。新增一条约束时，先试着写成守卫；只有当它确实依赖语义理解（比如"这段话是不是在陈述一个可验证的结论"）才交给评审员。这条边界一旦模糊，记忆编译会慢慢变成一个昂贵且行为不稳定的黑盒。

---

## 16. 常见误解

**"记忆里为什么没有我刚说的那件事？"**
候选证据在 `recent.jsonl` 里，但编译每 10 轮才跑一次，而且要过三道守卫和 Reviewer。查 `memory_history.jsonl` 能看到它是被 `deferred`、`rejected` 还是被某个守卫驳回了。

**"我能直接编辑 `durable.md` 吗？"**
能。它就是一个 Markdown 文件。但下一次编译会以它为基线做增量合并，所以你的编辑要符合模板结构（`validate_rendered_view()` 会检查）。

**"`memory_ops` 工具为什么调不到？"**
它不在 `agents/smith/config.yaml` 的 `tools.enabled` 白名单里。这是刻意的——记忆写入必须走证据裁决。

**"守卫会不会误拒正确的记忆？"**
会，而且设计上接受这一点。路径正则那个 `\b` 就是一处主动让步：粘着中文的路径干脆不检查，宁可放过也不误拒。误拒的代价是"这轮没记上，下轮再来"；漏拒的代价是"一条伪造的记忆永久留在基线里"。

**"两份视图为什么不合并成一份？"**
淘汰顺序、注入场景、生命周期都不同。合并后预算淘汰会在"用户偏好"和"项目状态"之间做无意义的取舍。

---

### 15.3 这一层的一句话总结

**记忆是两份被编译出来的渲染视图，每一句话都能回溯到一条真实发生过的证据。**

没有检索、没有向量、没有 episodes——两份文件整份注入 prompt，所以"Agent 记得什么"完全可以用文本编辑器打开确认。这是本地优先定位在记忆系统上的直接体现。

代价是容量受 prompt 预算硬约束（4 000 + 10 000 字符）。收益是**可预测和可审计**：你能看到它记了什么，也能通过 git 快照看到它什么时候记的、改了什么。

四层把关（三道确定性守卫 + 一道 LLM 评审）看起来重，理由只有一个：记忆的输出是它自己下一轮的输入，一次错误会被后续几轮不断强化，最终无法分辨来源。

---

## 17. 接下来

| 想深入 | 读 |
|---|---|
| 记忆视图怎么进 prompt（第 7 层和第 14 层） | [04 · Engine 核心执行](wiki-04-Engine-核心执行.md) §2 |
| 记忆维护的钩子怎么挂上去 | [04 · Engine 核心执行](wiki-04-Engine-核心执行.md) §7.2 |
| `sanitize_memory_text` 的密钥模式 | [06 · 安全与安全边界](wiki-06-安全与安全边界.md) |
| 编译走哪条 LLM 路由 | [07 · LLM 集成](../subsystems/25-LLM集成.md) |
| `/api/agent/memory/status` 返回什么 | [09 · Server API 层](../layers/43-Server.md) |
