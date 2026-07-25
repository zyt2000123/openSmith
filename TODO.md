# Agent-Smith TODO

> 维护规则：完成一项立即勾选；范围、依赖或验收标准变化时同步更新。
> **以代码为准**：任何条目与代码冲突时，先核实代码再改本文件。行号会随重构失效，
> 引用优先写符号名（`react_loop._will_compact`）而非行号。
> 当前目标：把单常驻 Agent 运行时补成一个完整 harness，不恢复多角色产品模型。
>
> 基线（2026-07-25 实测）：engine 523 passed / server 113 passed / shell 198 passed。
> engine 16.5k 行源码 + 13.4k 行测试，依赖方向 `server → engine → common` 已由 import
> graph 验证（`tool→execution`、`mcp→execution` 两处反向引用仅在 `TYPE_CHECKING` 内）。

---

## 已闭环

2026-07-10 的宏观评审（R1–R8）与内核深查（K1–K6）已收口，逐条状态见文末《评审条目归档》。
三批 P0 全部完成：

- **Rich Tool Contract** —— `engine/tool/interface.py` ToolDefinition：side_effect / 并发属性 /
  审批策略 / 执行环境 / timeout / idempotency，registry、policy、trace、审批 SSE 共用同一份元数据
- **Run State 状态机与 Checkpoint** —— `engine/execution/orchestration/run_state.py`：显式
  `RunStatus` + 合法转换表 + 原子保存；`tool_ledger` SQLite 幂等账本 + `_apply_crash_checkpoint`
  消费崩溃遗留 checkpoint 续跑
- **Permission / Approval / ExecutionEnvironment 分层** —— `safety/tool_policy.py` PolicyChecker 链
  （硬 guard 先于软挑战）、`sandbox/host.py` ExecutionEnvironment 协议、shell/git 进程执行收口到环境层
- **持久化 Trace** —— `observability/trace_store.py` 本地 JSONL，敏感键脱敏 + 深度/长度截断 +
  写入失败不阻断主任务；`recorder → projections → incidents → diagnosis → proposals` 归因链已通
- **取消与重启恢复** —— 两条路径都已接线并有测试：
  - *重启*：`RunStateStore.recover_interrupted()` 由 `server/app/main.py` 在启动时调用，把
    QUEUED / RUNNING / WAITING_APPROVAL 的遗留 run 清掉审批字段并转 `CANCELLED("server_restarted")`；
    `CANCELLED` 是可 resume 状态，配合 `tool_ledger` 幂等账本避免副作用重放。测试 `test_run_state.py`。
  - *消费者断连*：`_run_events_with_runtime` 的 `finally` 检查 `drained`，未跑完则写
    `CANCELLED("consumer_disconnected")` + `services.close()` + `APPROVAL_BROKER.cancel_run(run_id)`；
    首个事件前就断连走 `_cancel_unstarted_run`。测试 `test_runtime_contract.py`（4 处断言）。
  - **注意别误判**：`ApprovalBroker._pending` 是内存态属**有意设计** —— 挂起的 ReAct 帧本来就活不过
    进程重启，所以重启后转 CANCELLED 让用户重试是正确行为，不是缺陷。同理，取消不经
    `CancelledError` 而经 async generator 的 `finally`/`aclose()`，`grep CancelledError` 不是判据。

---

## 未闭环

### P1 · harness 完整性

- [ ] **工具调用串行执行**
      `react_loop` 拿到多个 tool_call 后是 `for tc in response.tool_calls:` 逐个 await。
      模型一次返回 3 个独立读取，要串 3 轮 I/O。
      **难点**：并行会破坏审批语义 —— 两个工具并发跑，一个触发审批阻塞时另一个可能已经写了文件。
      **验收**：`side_effect: none` 的工具并行、其余串行（Rich Tool Contract 的并发属性已声明
      `safe`/`serial`，目前无人消费）；补"并行读 + 串行写混合调用"与"并行中途触发审批"两个测试。

- [ ] **回归 Eval Harness**（原 P1 条目重定义）
      现状：完全没有。`safety/eval_guard.py` 只是 30 行敏感词检测，与 eval 无关（名字撞车）。
      13.4k 行测试测的是"代码没坏"，测不出"改了 prompt 层次或循环判定后 agent 变笨了"。
      **不采用公开 benchmark**：Terminal-Bench 2.0 / τ²-bench / SWE-bench 测的是解题排名，
      要 Docker + 每轮几小时几十美元，且与单常驻 Agent + 记忆 + 技能链的形态不匹配。
      **分两层做，不要混**：
      1. *任务式 e2e 冒烟*（参考 neovate-code `e2e/fixtures/`）：`{cliArgs: 自然语言, test: 断言}`
         配隔离工作区，真调 LLM，**断言世界状态而非模型文本**（文件真的被写了）+ 轮次上限。
         5–10 条，捕"harness 接线断了"。engine 现有 523 个测试全部 mock LLM，这一层是零。
      2. *trace replay 回归*：`observability/trace_store.py` 已经在落 JSONL，把真实运行的 trace
         固化成 golden case，重放时 LLM 走录制回放（不真调），断言决策序列不漂移。快、免费。
      **边界（别误用）**：trace replay 只在模型响应不变时有效 —— 改截断算法、压缩时机、门禁判据、
      路由规则、状态转换时管用；**改 prompt 内容或换模型时失效**，因为录制的响应由旧 prompt 产生，
      新 prompt 配旧响应这个组合从未真实发生过。它捕的是 harness 逻辑漂移，不是 prompt 质量。
      **验收**：第 1 层 5–10 条 e2e，真调 LLM，手动触发；第 2 层 30 条 golden trace
      （覆盖 outcome / trajectory / safety / recovery 四类），命令行入口，全量 <5 分钟。
      本项目不引 CI，第 2 层挂 `.git/hooks/pre-push`（非 pre-commit，太吵）；检索质量与
      过期记忆各占至少 3 条。

- [ ] **Hook 只有合并模式，没有生命周期切点**
      `HookType` 的五个值是 first / series / series_merge / series_last / parallel ——
      描述的是"多个 handler 的返回值怎么合并"，不是"在哪个时刻切入"。真正的切点硬编码在
      `agent_loop._ensure_memory_lifecycle_hooks` 里。
      **验收**：定义 PreToolUse / PostToolUse / SessionStart 一类生命周期点并对外可注册；
      memory 生命周期改为通过该机制注册，`agent_loop` 不再硬编码。

- [ ] **技能节点上下文交接**（原 R2，确认仍在）
      `skill/executor.py:_skill_conversation` 用 SKILL.md 顶替整个 system prompt，身份、记忆、
      工具策略、已装技能清单全部丢失；前序节点输出以 `Context: {context}` 的 dict repr 无预算灌入。
      **验收**：技能节点复用 `context/assembler.py` 的层叠结果（SKILL.md 作为 Workflow 层叠加，
      不是替换）；前序输出走预算裁剪；补"技能节点仍能看到身份与记忆"的断言。

- [ ] **Gate 判据不接执行事实**（原 R4 残余）
      LLMGate 的异常静默通过与 retry_hint 丢弃都已修（异常现在 fail，retry_hint 经
      `CTX_RETRY_HINT` 回流）。剩下的真问题是：门禁只读节点的 **output 文本**，读不到
      `tool_ledger` / trace 里的真实执行事实，所以模型自述"我已经跑过测试"就能过门。
      **验收**：gate 的 `context` 里带上本节点的工具调用事实（哪些工具、成功与否）；
      至少一个门禁改为以执行事实而非文本为判据。

### P2 · 结构债

- [ ] **`agent_loop.py` 1305 行 / 37 个顶层函数**
      8 个 `_bind_*_tool` 把内置工具接线硬编码进编排层 —— 加一个工具就要改 harness 里最该稳定的文件。
      **验收**：工具绑定改为声明式注册，`agent_loop` 只负责调度；文件降到 600 行以下且测试不变。

- [ ] **turn 级 ID 与 checkpoint 版本号**（原两处 `[~]`）
      run_id 与 tool call_id 有，turn 级 ID 没有（trace 靠 per-run 序列号兜底）；checkpoint
      原子保存有（临时文件 + 替换），显式版本号字段没有，坏格式只能靠解析失败识别。
      **验收**：Trace 支持 run → turn → tool call 三层查询；checkpoint 带版本号，
      跨版本读到旧格式显式拒绝而非猜测。

- [ ] **上下文压缩只有单一策略**
      `compress` / `compact_history` 是整段 LLM 摘要，没有"保留最近 N 轮 + 摘要更早"的分段策略，
      也不度量压缩造成的信息损失。无子代理意味着长任务只能靠压缩撑，这里是单点。
      **验收**：分段策略可配置；压缩前后对关键事实（文件路径、错误信息、决策）的保留率有度量并进 Eval。

- [ ] **四类数据的保留与删除规则**
      `PromptAssembler`（本轮 Context）/ Session Store（server 层对话历史）/ `RunStateStore`
      （执行与审批状态）/ `memory/`（跨会话事实）四者职责已分离，`memory/policy.py` 管视图策略。
      缺的是统一的 ID、生命周期、保留期与删除规则说明。
      **验收**：四类数据各写明 ID 来源、保留期、删除触发；有一条测试验证删除会话不留孤儿 run state。

---

## 已决策不做

- **K3 prune 单任务内不生效** —— 保持现状。让 prune 在任务中途生效会让模型丢失早期工具结果，
  风险高于收益；K1 修好 CJK token 估算后 compact 已能在正确时机兜底。
- **Sandbox Adapter** —— 不实现。工具声明 `sandbox` 而环境不匹配时 registry 显式拒绝
  （`error_kind=environment_unavailable`），不静默降级。
- **子代理 / 多 Agent 路由** —— 产品决策（见 CLAUDE.md）。代价：长任务只能靠压缩不能靠委派隔离。
- **工具结果缓存** —— 不做。`tool_ledger` 已负责重复调用决策，缓存属推测性优化。
- **External Agent Adapter、常驻多 Agent** —— 没有真实需求前不加。

---

## 全局约束

- [x] 保持依赖方向 `server → engine → common`（import graph 已验证，仅 TYPE_CHECKING 反向）
- [x] Engine 不依赖 FastAPI、HTTP 或产品实例管理概念
- [x] `agents/` 不 import 任何其他层（registry 经 `exec_module` 加载，契约是 `TOOL_META` + `execute`）
- [ ] Router 只做请求解析、调用和响应转换
- [ ] 不覆盖或清理当前工作树中的用户改动
- [ ] 所有行为变更遵循测试先行
- [ ] 每个阶段必须通过相关单测、全量测试和运行时验证

---

## 评审条目归档

2026-07-10 评审的图解与证据：https://claude.ai/code/artifact/e1368e55-bf8d-4f39-a805-0a72434e2865

| 编号 | 问题 | 状态 |
|---|---|---|
| R1 | `run_agent` / `run_agent_stream` 双实现漂移 | 已收敛，sync 版删除 |
| R2 | `execute_skill` 顶替整个 system prompt | **仍在** → 见 P1「技能节点上下文交接」 |
| R3 | `route_task_with_llm` 全仓库零调用方 | **仍在**（死代码，接线或删除二选一） |
| R4 | 门禁纯正则 / LLMGate 异常静默通过 / retry_hint 丢弃 | 后两项已修；判据不接执行事实 → 见 P1 |
| R5 | MCP stdio 每条消息 spawn/断开 | 已修，`mcp/session_pool.py` 按 session 复用 |
| R6 | checkpoint 只写不读 | 已接线，`_apply_crash_checkpoint` + `run_pipeline(start_node_idx)` |
| R7 | engine 硬编码 agents 层技能名 | 已修，pipeline 与 gate 均从 `agents/` 动态加载 |
| R8 | 硬截断切断 tool_use/tool_result 配对 → API 400 | 已修，切点回退到轮次边界 + 回归测试 |
| K1 | token 估算未考虑 CJK | 已修，汉字 1 字符/token |
| K2 | compact 摘要抹掉工具证据 | 已修，工具结果与调用意图纳入摘要输入 |
| K3 | prune 单任务内不生效 | 已决策保持现状 |
| K4 | prune/compress 污染调用方 history | 已修，逐条浅拷贝 |
| K4.1 | 长度截断后工具调用会提交陈旧 draft | 已修，撤回全部未提交 draft |
| K5 | 4xx 也重试 | 已修，仅 429 与 5xx 重试 |
| K6 | 锁定 OpenAI 兼容协议 | 已修，`AnthropicAdapter` 原生 `/v1/messages` + Gemini adapter |
