# Engine 全链路白盒地图

一次用户输入从进入 engine 到产出回复，经过哪些节点、每个节点改变了什么、**留下了什么记录**。

本文按代码核实，不按既有文档转述。每个节点标注 `文件:行`。与其他 `docs/` 文档冲突时以本文与代码为准。

---

## 一、主干：一次对话的完整路径

```
Ink shell ──HTTP POST /sessions/{id}/messages──▶ server/app/services/session_service.py
                                                          │
                                    最近 40 条历史（_HISTORY_LIMIT）
                                                          ▼
                       engine/execution/orchestration/lifecycle.py
                       run_stream_with_runtime()  :375
                         ├─ RunStateStore.create()          ← 持久化 run 记录
                         ├─ _start_event_boundary()  :345   ← 建立唯一记录边界
                         └─ ToolExecutionLedger()           ← 工具幂等台账
                                                          ▼
                       _run_events_with_runtime()  :587   ← 生成器，全程 try/except/finally
                                                          │
                        ┌─────────────────────────────────┤
                        ▼                                 │
              prepare_runtime()  (preparation.py)         │
                ├─ route_task()   纯词法关键词匹配         │
                ├─ assemble_prompt()  16 层拼装            │
                └─ 返回 setup(system_prompt, route, chain) │
                        │                                 │
                        ▼                                 │
        with generation_context(run_id, session_id),      │
             llm_purpose("main"):          :628-630       │
                        │                                 │
                        ▼                                 │
              run_agent_stream()  (agent_loop.py:60)      │
                ├─ forced_skill        → _run_forced_skill_stream
                ├─ 无 pipeline/chain   → react_event_loop   ← 绝大多数请求
                └─ 有 pipeline         → SkillChain 逐节点 + 门禁
                        │                                 │
                        ▼   （每个事件）                    │
              await boundary.record(event)  :669  ◀───────┘  先记录
              yield event                   :670             后下发
                        │
                        ▼
              finally:  :696
                ├─ with generation_context(...):  ← 修复点，见 §4
                │    _persist_runtime_learning()  → 记忆编译
                ├─ hook_registry.run_stop_hooks()  ← 成本跟踪
                └─ services.close() / APPROVAL_BROKER.cancel_run()
                        │
                        ▼
              RUN_FINISHED  :780-782（先 record 后 yield）
                        │
                        ▼
              SSE ──▶ shell
```

---

## 二、每个节点：做什么 / 记录什么

### 2.1 提示词装配 — `context/assembler.py`

16 个带信任标签的层，顺序固定：

| # | 层 | 来源 |
|---|---|---|
| 1-3 | Agent Role / Style / Workflow | `profile:role.md` 等 |
| 4 | Tool Usage Policy | engine 内置 |
| 5 | Available Tools | `tool_registry:enabled` |
| 6 | Available Skills | `skill_registry:enabled` |
| 7 | Learned User Context | `context.md` |
| 8-9 | Global / Project Instructions | CLAUDE.md 类 |
| 10 | Identity Guidance | identity yaml |
| 11 | Evaluation Safety Guidance | **条件加载**（`EVAL_SENSITIVE`） |
| 12 | Output Style | `agents:output_style.md` |
| 13 | Memory Governance | engine 内置 |
| 14 | Durable Memory | `memory/durable.md` |
| 15-16 | Runtime Context / Engine Runtime Control | `RUNTIME_ONLY` |

**记录**：`boundary.append_prompt_manifest()`（`lifecycle.py:626`）把层清单与来源写进 trace。提示词内容本身不入 trace，只留 provenance。

> 记忆是**两个渲染视图整份注入**，没有检索、没有索引、没有 embedding。`context.md` 4000 字符、`durable.md` 10000 字符，由 `MEMORY_POLICY.md` 封顶。

### 2.2 路由 — `execution/routing/task_router.py`

纯词法：`IdentityCatalog` 关键词/示例匹配 + 优先级。**无 LLM 兜底**（曾有，已删——见 commit `98c7e1c`）。路由不能凭空造出身份、领域或 pipeline。

### 2.3 上下文裁剪 — `context/fitting.py:68 fit_request()`

每轮请求前跑一次，决定模型实际看到什么：

```
tool schemas 单独超预算  → UNFIT_TOOL_SCHEMAS
system 前缀单独超预算    → UNFIT_STATIC_PROMPT
prune_tool_outputs()     → actions += pruned_tool_output_chars:N
活跃上下文单独超预算      → UNFIT_REQUEST
< compaction_trigger     → FIT / COMPACTED（早返回，安全：trigger = safe_budget × 0.85）
compact_history()  LLM   → actions += compacted_history / compaction_failed / compaction_rejected
仍超预算 → trim_conversation_for_context_limit() → actions += deterministic_trim
                              ↓
              硬后置条件复核  :207-210，不达标 → UNFIT_REQUEST
```

**记录**：`CONTEXT_COMPRESSION_START/END` + `CONTEXT_USAGE`（13 个容量字段 + `fit_status` + **`actions`**）。UNFIT 时额外发 `INCOMPLETE`，携带完整诊断。

> `actions` 原先只在 UNFIT 路径进 trace，成功但有损的路径不记——已修，见 §4。

### 2.4 工具执行 — `react/react_loop.py:1066+`

```
TOOL_CALL_START  :1066   ← 发在 policy.evaluate() 之前，之后每条出口都有配对
        │
   policy.evaluate()
        ├─ challenged        → TOOL_CALL_RESULT{preflight:True}      :1072  （软挑战，可重试）
        ├─ approval_required → TOOL_CALL_RESULT{approval_required}   :1109
        │       └─ 用户拒绝/超时 → TOOL_CALL_RESULT{approval_outcome} :1149
        └─ allowed
              ├─ ToolGuard        ← 非绕过边界，锚在 common/paths.py
              ├─ PreToolHook      ← 可阻断
              ├─ execute
              └─ PostToolHook     ← 只告警
```

一次 START 在审批路径上对应**两条** RESULT，`run_state.py` 的投影对此有专门处理（`request_approval` 原子清 `current_tool`，保证 `event_seq` 只加一次）。

### 2.5 记忆 — `memory/`

事件累积在 `memory/recent.jsonl`；`compile_durable()` 按 `.compile_offset` 增量归并；每 10 轮（`_COMPILE_INTERVAL`）触发一次编译；Dream 只做净化与回收过期前缀。

编译走 generator-evaluator 双模型评审（最多 3 轮）。`_read_view()` 会在「消毒后整份文档被清空」时抛 `MemoryViewUnreadableError` 而非当作空文档覆盖——这是 commit `884569a` 修的 P0。

---

## 三、观测：三条通道，一个记录边界

```
                    ExecutionEvent
                          │
        ┌─────────────────┴──────────────────┐
        ▼                                    ▼
_RunEventBoundary.record()          （lifecycle 自建的 7 个终态事件
  (lifecycle.py:286)                  同样先 record 后 yield）
        │
        ├─▶ project_execution_event()  → RunStateStore   【控制面】能否恢复/是否待审批
        │      守卫：except (RunStateError, ValueError, TypeError)
        │      OSError 已在 run_state.py:643 包成 RunStateError，守卫是够的
        │
        └─▶ RunEventObserver.record()  → RunEventRecorder (observability/recorder.py)
               ├─▶ TraceStore.append()     【审计面】哈希链，RUN_FINISHED 时 seal()
               ├─▶ RunSummaryProjection    【聚合面】事件计数/工具数/回退数/token
               └─▶ summary_sinks           （仅 RUN_FINISHED 时触发）

               独立通道：GenerationRecord → emit_generation()
                 每次模型调用一条，purpose ∈ {main, gate, compact, memory}
                 scope 由 generation_context(run_id, session_id) 提供
                 → server/app/main.py:102 装 TokenStatsService.record_generation
                 → llm_generations 表
```

**三条通道靠 `run_id` 关联**（contextvar 传递），成本靠 `llm_purpose` 归因。

### 无盲区的三条硬保证

1. **没有事件绕过记录边界。** react_loop / pipeline / agent_loop 的全部 119 个 yield 点都汇入 `lifecycle.py:669`；lifecycle 自建的 7 个终态事件各自 `record` 在 `yield` 之前。
2. **终态在每条出口都发。** 正常与失败走 `drained=True` → `:780`；消费者提前断开走 `drained=False` → `:726` 补一条 `CANCELLED`；启动期失败走 `_failed_setup_stream` → `:501`；未启动即关闭走 `_cancel_unstarted_run` → `:454`。
3. **记录失败不杀 run。** trace append / seal、projection、summary sink 全部各自 try 包裹并降级为 warning。

---

## 四、本轮审查修掉的观测盲区

| 盲区 | 症状 | 修复 |
|---|---|---|
| 记忆编译的模型调用无归属 | 跑在 lifecycle 的 `finally` 里，已出 `generation_context` 作用域 → `llm_generations.run_id` 为 NULL。而它每 10 轮触发、是最大的侧路成本 | `lifecycle.py:703` 重新进入作用域 |
| `/compress` 无归属 | 不属于任何 run，从未进过作用域 → `session_id` 为 NULL，尽管调用方手里就攥着 session_id | `session_service.py` 包一层；`run_id` 保持 NULL（它确实不属于任何 run，这是诚实值） |
| 裁剪动作不入 trace | `fit_status="compacted"` 既可能只剪了工具输出，也可能整段历史被摘要替换；`recovered` 意味着消息被直接删除。`actions` 只在 UNFIT 时才记 | `CONTEXT_USAGE` 两个发射点都带上 `actions` |

第三条的实际代价：**「Agent 怎么把我开头那个问题忘了」曾是 trace 唯一答不上来的问题。** 另外 `compaction_failed` 会把 `model_compaction_enabled` latch 成 False、改变该 run 之后所有轮次的行为，这个状态跳变原先也完全不可见。

---

## 五、不变量（改动时不要破坏）

- `engine/` 不认识 FastAPI、HTTP、agent 实例管理
- `agents/` 不 import 任何其他层；工具契约是 `TOOL_META` + `execute`，不是类型
- `common/paths.py` 是运行时数据根的唯一真相，`tool_guard.py` 的非绕过写保护锚在它上面
- 硬守卫（`tool_guard`）永远在软挑战（`fact_gate`）之前——有测试锁定
- 记录边界只有一个：任何绕过 `lifecycle` 的输出通道都会直接变成观测盲区
- 路由是纯词法的；不存在 LLM 兜底分类器
