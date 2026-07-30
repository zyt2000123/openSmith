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

### P0 · 收敛为单一 Smith 身份 + Coding 能力域，并闭合三条 SkillChain 的意图入口

- [ ] **设计已定，尚未实施。** Smith 是唯一常驻的 session identity、人格偏好与记忆归属；
      Coding 不是第二个会话身份，而是 Smith 之下可路由的能力域。当前
      `agents/identities/coding.yaml` 与 `smith.yaml` 仍是平级 identity，且
      `prepare_runtime()` 会在命中链路时把一次 Smith 会话路由为 `coding`，与该模型不符。
      这项 P0 不恢复常驻多 Agent；未来如需 subagent，也只能继承 Smith 根身份并领取受限的
      Coding capability profile，不能成为与用户对话竞争的人格。

      **目标契约：**
      1. `RouteDecision`、session/checkpoint、SSE 与 Shell 都区分 `identity_id=smith`、
         `domain_id=coding | None`、`pipeline_id`。直接 ReAct 没有 domain/pipeline；进入链路时
         Smith 身份保持不变，Coding 的工具、skills、gate/condition 只在该能力域内生效。
      2. Coding 域只暴露三条固定链：需求调研
         `grill-me → grilling → research → ecc-plan`；TDD 开发（新功能和 bug 修复，按条件先过
         `diagnosing-bugs`）`→ tdd-workflow → verification-loop`；代码审查
         `code-review → verification-loop`。不要为普通 ReAct 再造第四条隐式 pipeline。
      3. 意图入口分层且可解释：显式入口/菜单强制进入指定链；规则命中或高置信语义识别自动进入；
         歧义或未识别则保留 Smith 的直接 ReAct，并给出不阻塞的可选入口。现有
         `route_task_with_llm` 不能继续处于无调用方状态：要么作为受声明 route 限制的第二层分类器
         接入真实运行时并测试，要么删除；不得让模型凭空发明 identity、domain 或 pipeline。
      4. 一旦开始链路，`awaiting_user_input`、断线重连和重启恢复必须按已保存的
         `domain_id + pipeline_id + node` 续跑，不能重新识别意图；为历史
         `identity_id=coding` checkpoint/session 提供显式迁移（到 `smith/coding`）或安全、
         用户可见的拒绝与重启路径。

      **验收：**
      - runtime catalog 中只有 Smith 可作为 session identity；普通对话从开始到结束均呈现 Smith。
      - 三条链均可通过显式入口启动，也可由覆盖同义自然语言的路由测试识别；每条链的事件、
        transcript 与暂停后续跑均显示 `smith + coding domain`，不再显示身份切换为 `coding`。
      - 未识别、低置信和混合意图不会中断对话或误启危险链路，稳定退化为直接 ReAct；用户随后
        选择显式入口仍可进入正确链路。
      - 覆盖 catalog/schema 迁移、task router、pipeline checkpoint、Engine SSE、Server session
        持久化与 Shell 展示；旧 checkpoint 的行为有回归测试，三条链不因迁移丢失其内置 skill/tool。

### 已修 · 真实运行暴露的故障（保留全过程，因为它是 e2e 层价值的唯一实证）

- [x] **工具结果回灌后偶发 HTTP 400** —— 2026-07-25 由 e2e 冒烟抓到并当天闭环。
      修复：`react_loop` 回灌 assistant 消息时，`response.reasoning` 非空则附加
      `reasoning_content`。非推理模型 reasoning 为空 → 字段不出现，同一条路径同时支持两类
      模型，无需探测模型能力。回归：`engine/tests/execution/test_reasoning_roundtrip.py`（3 条，含
      多轮各自回传与非推理模型不多字段）。验证：e2e append 连续 5/5 通过（修前约 50% 失败）。
      现象：`server/tests/test_e2e_smoke.py::test_e2e_smoke[append]` 轨迹为 `read_file` →
      **下一次 LLM 请求 400**，流式（`adapters/openai.py:196`）与非流式回退
      （`adapters/_http.py:117`）双双 400，端点 `/v1/chat/completions`。
      **间歇**：同一条 case 在前一轮全跑里 PASSED，只调 `read_file` 的 `read` case 也稳定通过。
      **根因已确认**（2026-07-25，循环跑 append 第 2 次复现，provider 错误原文）：
      `OpenAI bad request: The 'reasoning_content' in the thinking mode must be passed back
      to the API.`（`type: invalid_request_error`）

      推理模型（当前 `deepseek-v4-pro`）返回 `reasoning` + `tool_calls` 后，engine 把 assistant
      消息回灌进下一轮 conversation 时丢掉了 reasoning，provider 因此拒收整个请求。
      间歇性来源：只有该轮真的产生了 reasoning、且后面还有请求（工具调用后继续）时才触发。
      **修复要点（涉及抽象边界，别图快）**：`ChatResponse.reasoning` 是 engine 的中立字段，
      `reasoning_content` 是 wire format。正确分工是 conversation 存中立的 reasoning，由 adapter
      在构造请求体时翻译（OpenAI 兼容/deepseek → `reasoning_content`，Anthropic → `thinking`）。
      **不要**在 `react_loop` 里直接写 provider 字段名。
      落点：`react_loop.py` 追加 assistant 消息处（`"tool_calls": [` 一带）+ `adapters/openai.py`
      的消息翻译处。
      **留给后来人的教训**：636 条 mock 测试全绿却漏掉它 —— 因为 mock 的 LLM 从不校验请求体
      合法性，而这个 bug 的全部内容就是"请求体不合法"。这不是测试写得不够多，是 mock 层的
      结构性盲区，再加 600 条也一样漏。这也是 e2e 那 5 条存在的全部理由。

### P1 · harness 完整性

- [ ] **工具调用串行执行**
      `react_loop` 拿到多个 tool_call 后是 `for tc in response.tool_calls:` 逐个 await。
      模型一次返回 3 个独立读取，要串 3 轮 I/O。
      **难点**：并行会破坏审批语义 —— 两个工具并发跑，一个触发审批阻塞时另一个可能已经写了文件。
      **已量，结论：暂不做**（2026-07-25 录制 e2e 五条，11 个模型回合）：

      | 一次返回的 tool_call 数 | 回合数 | 占比 |
      |---|---|---|
      | 0 | 3 | 27% |
      | 1 | 8 | 73% |
      | **>1** | **0** | **0%** |

      `deepseek-v4-pro` 从不一次返回多个 tool_call → **并行执行的收益为零，没有可并行的对象**。
      Claude Code 做这个优化是因为 Claude 经常一次返回多个 tool_use，那是模型行为不是通用事实。
      顺带测到：11/11 回合都带 reasoning（100%），印证上面 reasoning_content 那条修复的必要性。
      **重测触发条件**：换成 Claude/GPT 一类模型，或换 provider 后。样本仅 11 回合且任务简单，
      复杂任务可能不同；重测用 `AGENT_SMITH_RECORD_LLM` 录真实会话即可。
      **原局限已消除**（同日）：测量时录制强制非流式，同批 e2e 流式 5/5、录制模式 2/5。
      改成流式保真录制后录制模式回到 5/5 —— 差异确实来自强制非流式，不是模型随机。
      上面 11 回合的样本是非流式录的，占比结论仍成立但样本偏小；换模型后按流式重录即可
      （流式录的是事件序列，统计多工具需从 `response.function_call_arguments.delta` 解析）。

      **参考实现**（claude-code 快照 `src/services/tools/toolOrchestration.ts`，188 行）：
      1. `partitionToolCalls` 把一批 tool_use 分成 batch：要么「1 个非并发安全工具」，
         要么「多个**连续**的并发安全工具」。**保序，不重排** —— 「读 a、写 b、读 c」不会把
         a 和 c 并到一起跨过 b。这是最容易做错的一点。
      2. `isConcurrencySafe(parsedInput)` **接收参数**，按实际调用判定而非按工具类型
         （同一 shell 工具 `ls` 安全、`rm` 不安全）。Agent-Smith 已有更细的等价物：
         `fact_gate._is_read_only_shell/_is_read_only_git/_is_read_only_sed`，一份判定两个用途。
      3. **三重保守**：参数解析失败→不安全、判定抛异常→不安全、未声明→默认不安全。
      4. 并发跑法 `all(generators, getMaxToolUseConcurrency())` —— 有并发上限，不是无限 fan-out。
      5. 并发批内的 context 修改**排队到批结束后统一应用**，避免并发写竞争。
      **审批不必并行**：`ApprovalBroker._pending` 本就支持多 pending（每个独立 Event），但
      `RunState` 只有单个 `approval_id`。不用改 —— Claude Code 靠「需要审批的工具天然不进并发批」
      消解问题，而非并行审批。并行审批还会引入"拒绝其一时另一个已落盘"的无回滚状态。
      **验收**：`side_effect: none` 的工具并行、其余串行（Rich Tool Contract 的 `safe`/`serial`
      已声明无人消费）；测试覆盖"读写混合保序分批"、"判定抛异常时退化为串行"、"并发上限生效"。

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

- [x] **技能节点上下文交接**（原 R2）
      `pipeline` 传递已装配的 `base_messages`；`skill/executor.py:_skill_conversation` 通过
      `PromptLayer` 追加 SKILL.md Workflow 层，不替换身份、记忆或工具策略。前序 `*_output`
      与 gate feedback 以有界、带不可信参考围栏的 handoff 传递，不再序列化整个内部 context。
      回归：`test_skill_context_handoff.py` 断言第二个技能节点仍能看到身份、记忆、工具策略和
      Workflow 内容，且前序输出被截断、`_state_dir` 不泄露。

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
| R3 | `route_task_with_llm` 全仓库零调用方 | **仍在** → 见 P0「单一 Smith 身份 + Coding 能力域」 |
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
