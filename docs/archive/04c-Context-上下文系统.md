# 04c · Context 上下文系统

> **已归档 —— 不是当前事实。**
> 本文已被 [22 · 上下文治理](../22-上下文治理.md) 取代；两者冲突时以那一篇和源码为准。
> 保留在此仅供追溯当时的设计取舍，不再随代码更新。


## 本章结论

`engine/context/` 的职责不是“把文字拼成 prompt”，而是在模型上下文窗口有限的前提下，按来源可信度和任务时效性选择、压缩并计量信息。它是模型调用前的容量控制器，也是指令注入的边界。

## 总体架构

```mermaid
graph LR
    I[History / 指令 / 记忆 / Tool schema] --> A[PromptAssembler]
    A --> P[PromptPlan + Manifest]
    P --> F[fit_request]
    F --> B[ContextBudget]
    B --> C[prune / compact / hard trim]
    C --> M[LLM messages + ContextReceipt]
```

## 目录与职责

| 文件 | 解决的问题 | 核心对象/函数 |
| --- | --- | --- |
| `assembler.py` | 何种信息能以什么信任级别进入系统提示 | `PromptLayer`、`PromptPlan`、`PromptAssembler` |
| `budget.py` | 如何估算 messages 与 tool schema 的 token 消耗 | `ContextBudget`、`context_budget_for()` |
| `fitting.py` | 预算不足时如何决定是否可发送 | `fit_request()`、`ContextReceipt` |
| `compression.py` | 如何先压工具输出，再压历史，最后硬裁剪 | `prune_tool_outputs()`、`compact_history()` |
| `summary.py` | 如何将旧会话整理为可验证摘要 | `summarize_session()` |

## 调用链

```text
RuntimeContext / request → PromptAssembler → PromptPlan
PromptPlan + history + tools → measure_request → fit_request
fit_request → prune_tool_outputs → compact_history → trim_conversation_for_context_limit
fitted messages → ReAct / LLMClient → ContextReceipt / CONTEXT_USAGE event
```

## 核心设计

### 1. Prompt 分层与信任标签

`PromptLayer` 同时记录 source、authority、trust、scope 和 load reason。设计意图是让 Agent role、工具策略、用户指令、项目指令、identity guidance 与可能不可信的外部内容不再只是一串字符串：装配器可以解释一层为什么存在、来自哪里，以及它能影响什么。

| 问题 | 输入 | 输出 | 失败与边界 |
| --- | --- | --- | --- |
| 指令冲突 | 多来源 Markdown/YAML 与运行时能力 | 有序 prompt manifest | 低可信内容不能伪装为平台或安全指令 |
| 上下文不可解释 | 隐式字符串拼接 | 可检查 `PromptPlan` | 调试时可追溯层的来源与装载原因 |

### 2. 预算不是单一阈值

`ContextBudget` 同时考虑模型限制、消息正文和 tool schema。`measure_request()` 先形成测量结果，`fit_request()` 再决定是否执行压缩；结果以 `ContextFitResult` 和 receipt 返回，而不是在下游静默截断。

### 3. 三段式降载

```text
请求超预算 → 裁剪可压缩工具输出 → LLM 摘要压缩历史 → 保留头尾与活跃请求的硬裁剪
```

工具输出优先被裁剪，因为它通常体积大且可重取；活跃请求被保留，避免压缩把当前待完成的任务删掉。若 provider 因上下文上限拒绝，ReAct 还能触发一次受控恢复，而非无限重试。

## 源码阅读顺序

1. `assembler.py`：先看 `PromptLayer` 与 `PromptAssembler`，理解哪些数据能进入 prompt。
2. `budget.py`：再看 token 估算和模型限制如何得出预算。
3. `fitting.py`：查看接近/超过预算时的决策结果。
4. `compression.py`：最后查看每个压缩阶段保留什么、丢弃什么。

## 测试与边界

- 修改 prompt 层时，验证信任顺序与 manifest，而不是只比较最终大字符串。
- 修改压缩时，验证活跃用户请求、工具调用配对和摘要结构不丢失。
- token 估算是预算控制，不是 provider 的精确计费；真实 usage 以 LLM 规范化记录为准。

## 自测题

1. 为什么 tool schema 也必须计入上下文预算？
2. 为什么硬裁剪必须保留活跃请求，而不能只保留最近 N 条消息？
