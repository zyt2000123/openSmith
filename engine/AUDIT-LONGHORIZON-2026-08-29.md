# engine 长程能力对抗性审查（2026-08-29）

**方法**：7 维度并行审查 → 每条发现由独立反驳者重读代码/写仿真脚本证伪 → 分级。
26 条原始发现，去重后 22 条，**20 条确认（1 P0 / 10 P1 / 9 P2），2 条证伪**。
本文件是打补丁的工作清单；分支 `fix/engine-patch`。
每条补丁必须附回归测试——所有确认项的共同特征就是"该行为无测试锁定"。

---

## 落地状态（截至 2026-08-30，`b3253d9..ab7f03a` 共 11 个提交）

**20 条全部落地**，另加一轮对抗性 review 发现的 4 条补丁自身缺陷。

| 状态 | 条目 |
|---|---|
| 已修 | #1–#20 全部 |
| 部分保留 | **#15** 只做了 state/head/torn 清理；`DELETE FROM tool_executions` **未做** —— 删除副作用审计账本是策略决定，不该在补丁里顺手做，需单独立项 |

### review 发现的补丁自身缺陷（已修）

- **P0 回归**：#1 把 head 从固定 `conversation[:2]` 改成按角色选，拆掉了一个没人写下来的不变量（`[system, *历史, 请求]` 的 index 1 恰好是 user 轮）。裁出的对话可能以 assistant 开头，Anthropic 直接拒收且该错误不触发重试 —— 历史顶到 40 条上限的会话**每一轮第一次 provider 调用就炸**。切点增加"必要时前移到 user 轮"的对齐。
- **#10 的 (b) 是承重的**，不是可选的另一半：删 state 让复活循环收敛了，但升级后**第一次**启动仍会把孤儿 state 盖上"刚刚"的时间戳，在同一轮对账里把真实历史全挤出保留窗。恢复路径已改用 `state.updated_at`。
- **保留策略会删掉正在恢复执行的 run 的状态文件**：删除动作收进 `RunStateStore.prune()`，由状态的拥有者拒绝删活跃 run（同时消掉跨模块硬编码文件名耦合）。
- **估算用量冒充精确计费数据**：#8 的兜底事件在存储层不可分辨，中文会话面板高估约 3 倍。已用 `source_key` 命名空间标注来源与可信度。

### review 发现"锁不住修复"的测试（已补强）

学习收尾上限（墙钟判据永真）、compaction 闩锁（只锁下界）、SSE 上限接线（只测纯函数）、`TOOL_CALL_START` 同步 fsync（无人看守）、pipeline base gate 的 infra 分支（与 domain 分支是两处独立实现）、#10 的级联结果（只测了删文件这个手段）。

### 顺带删掉的假旋钮

`SSEStreamLimiter.max_duration_seconds`：adapter 只从 `LLMProviderConfig` 构造，而那个 dataclass 没有路由字段，所以它只能永远是默认值。接上它得先加一个没人要求的 config 字段；留着则让读代码的人以为路由已被考虑过。

### 文档待更新

CLAUDE.md §9 测试基线（现为 engine 1211 / server 247+5 skipped）、§9 Seatbelt 段落重复、§6b「Spend is bounded twice」需补估算兜底、§6a cost-tracker 现已真通、§5 需记一条 observability→execution 的新依赖边。

---

## P0（长会话确定性触发，丢失正在执行的指令）

### 1. 硬裁剪丢掉当前用户请求
- **位置**：`engine/execution/react/react_loop.py:485-510`（`CONVERSATION_KEEP_HEAD=2`，`engine/execution/react/budget.py:16-18`）
- **机制**：消息数超 40 时保 `conversation[:2]` + 最近 28 条。注释以为 head 是 "system + initial user"，但生产布局是 `[system, *history, 请求]`（`agent_loop.py:87-91`，server 侧 `_HISTORY_LIMIT=40`）——有历史时 index 1 是最老历史消息，当前请求在尾部，多次裁剪后被静默切掉。
- **复现**：反驳者用真实 `react_event_loop` + FakeLLM 仿真：43 条起步，第 4 次裁剪起请求消失，模型看着几天前的旧消息继续跑满 max_iters，全程无日志。级联：请求丢失后 `_split_active_context` 误判 active turn → `compact_history` 原样返回 → `compaction_rejected` → 本 run LLM 压缩被永久误禁（见 #12）。
- **补丁**：裁剪时用 `_split_active_context` 同款判定定位最新真实 user turn（跳过 RUNTIME_USER_NOTE_PREFIX），把 [前导 system + 该请求] 钉进不可裁剪区；或把条数硬切并入 `fit_request` 让请求享受 active turn 同等保护。
- **测试缺口**：`test_react_budget.py:1385/:1508` 全部把请求放 index 1，无 `[system, history..., request]` 布局用例。

## P1（常见长程场景触发，明显质量退化 / 预算失控 / 数据被清）

### 2. 二次 compaction 时旧摘要被 `content[:2000]` 头部截断
- **位置**：`engine/context/summary.py:201-203`；注入点 `compression.py:315-326`
- **机制**：上一轮摘要以 user 消息注入（前缀 fence ~400 字符）；再次 compaction 时它落入 history 段被无条件截到 2000 字符——五段式 XML 摘要排在末尾的 `<recent_actions>`/`<current_plan>` 整段丢失，每多一轮 compaction 复利一次。server 注入的 `[Session context summary]` 走同一条截断。反驳者端到端复现确认。
- **补丁**：对 `[Previous conversation summary]` / `[Session context summary]` 开头的 user 消息免除 per-message 截断（超预算交 `_fit_summary_request` 统一裁）。

### 3. 审批拒绝/超时路径缺同错熔断
- **位置**：`engine/execution/react/react_loop.py:1148-1182`
- **机制**：拒绝/超时只 `consecutive_errors += 1; continue`，不更新 `last_error_key`/`identical_error_count`（tool_disabled / pre-hook / 工具报错三条路径都有）。ToolPolicy 无拒绝记忆，模型重发同一调用就再弹窗再等 300s。上界仅 `MAX_FAILED_TOOL_RECOVERY_ITERS=20`：最坏 20 次弹窗轰炸 / 无人值守挂 100 分钟。
- **补丁**：给该分支加与 pre-hook 阻断相同的同错熔断（错误键 = tool + denial 文本）；同 run 内相同 (tool, args) 的再次审批直接沿用上次拒绝结论。

### 4. `compile_context` 缺 Policy 6.2 跳批，context 游标可被永久钉死
- **位置**：`engine/memory/compile.py:641-643`（对照 durable 侧 `:729-734`）
- **机制**：durable 连续 3 次 deferred 会 `_skip_evidence_batch`；context 的 except 分支只记录就 raise，游标永不推进。级联三连：Dream 回收上限 = `min(两游标)`（`dream.py:369`）→ recent.jsonl 永远无法回收；卡住批次占满 24k 前缀 → context.md 冻结；无 cooldown → 每 10 个入账轮次白烧最多 3 次 generator 调用，无限期。双维度独立发现并确认。
- **补丁**：`_skip_evidence_batch` 参数化 view（`_write_offset` 已支持），except 分支加对称的 `deferred_streak(memory_dir, "context") >= 3` 跳批；镜像补 context 版 skip 测试。

### 5. 保留守卫挡住同类证据演化：新决定/新偏好永远替换不了旧条目
- **位置**：`engine/memory/_guards.py:234-249`（`_FORGET_KINDS` 仅 {forget, correction}）
- **机制**：结论 section 的 replace/remove 一律要求 forget/correction 证据，但 Policy §1.4 又强制"同主题更新须替换"。kind=decision/preference 的合法演化三条路全死：replace 被拒、同键 add 撞 `topic_already_exists`、换键 add 留矛盾双条目。3 轮 deferred 后 durable 侧跳批把用户明确说的新决定**静默丢弃**——"决定改用 Postgres"永远写不进，durable.md 永远留着 SQLite。
- **补丁**：允许同类或更高优先级的用户显式证据（decision/preference）更替对应结论条目，继续拒绝 work/partial_work；补"新决定可更替旧决定"测试。

### 6. 记忆评审窗口 32k < 生成输入 34k：证据中段被截，合法变更被判 fabrication
- **位置**：`engine/memory/_review.py:159`（`_MAX_REVIEW_SOURCE_CHARS=32_000`）；组装 `compile.py:396-402`
- **机制**：durable 满载时 review_source ≈ 10k(existing) + 24k(source) > 32k，`_truncate_source` 保头保尾——被省略的中段**按构造全部落在证据块内**。守卫用完整 source 建索引所以放行，reviewer 在可见文本里找不到引文 → 按 HARD FAIL 判 fabrication → rejected（不计 deferred、不 cooldown、不跳批）→ 每回合 ~6 次 LLM 调用无限重试。
- **补丁**：分段限额——PRIOR ACCEPTED MEMORY 可截，SELECTED NEW EVIDENCE 一个字符不许截（它是 quote 校验的 ground truth）；或 `_MAX_REVIEW_SOURCE_CHARS` ≥ 两部分预算之和。补"双满载时 reviewer 看得到全部证据行"测试。

### 7. `~/.agent-smith` 快照 git 仓库无界增长，commit 永不触发 gc
- **位置**：`engine/memory/_snapshot.py:34`（TRACKED_VIEWS 含 recent.jsonl）、`:53-60`（同步 subprocess）
- **机制**：每次接受写入/sanitize/Dream 前后都全量提交 recent.jsonl（数 MB 级新松散 blob）；git 操作只有 init/add/commit/log/checkout，实测 commit 不触发 auto-gc——一年可达 GB 级、objects/ 十万文件，且同步 `subprocess.run`（15s 上限）跑在事件循环线程上，越大越卡。注意：被回收证据可从 git 历史恢复是**测试锁定的故意设计**（`test_memory_snapshot.py:247`），补丁不得破坏可恢复语义。
- **补丁**：Dream 周期里低频跑 `git gc --auto`（沿用 _RUN_CONFIG/超时/失败仅告警）；评估 recent.jsonl 只在 Dream 回收前后两次快照中提交。

### 8. 流式 relay 不回报 usage 时零兜底：sub-agent 双预算全部失效
- **位置**：`engine/execution/react/react_loop.py:713-715`；计费口 `subagent/runner.py:260-274`
- **机制**：中转站忽略 `stream_options.include_usage` → 全程无 usage chunk → `_usage_event_data(None)` 全零 → 整轮不发 TOKEN_USAGE → `batch.spend` 从不发生，400k/agent 与 600k/batch 全灭，只剩 600s 超时兜底。CLAUDE.md "Spend is bounded twice" 的声明在此形态下不成立。
- **补丁**：`usage_reported==0` 时用 `fit.receipt.estimated_input_tokens` + 输出长度合成带 `estimated: true` 的 TOKEN_USAGE 事件；补"provider 全程不回 usage 仍能耗尽预算"测试。

### 9. SSE 硬顶（1 万事件 / 15 分钟）把合法长流打死且回撤已渲染文本
- **位置**：`engine/llm/adapters/_http.py:32-33, 66-72`
- **机制**：超限抛 `LLMResponseError`，openai adapter 不捕、`saw_content_event=True` 时禁止重试 → react_loop 逐个 PROVISIONAL_RETRACT 已渲染文本后 raise，run 失败；usage chunk 在流尾永远收不到，上万已计费 token 记零。推理模型 + 大 `max_output_tokens` 配置下单轮可达。
- **补丁**：上限与配置联动（事件上限 ≥ `max_output_tokens*2`，background 路由放宽时长）；或超限改为合成 `RESPONSE_COMPLETED(finish_reason="length")` 让既有 length-continuation 接管，不丢已产出内容。

### 10. 保留策略修剪过的 run 每次重启"复活"成僵尸 summary，级联挤掉全部真实 trace
- **位置**：`server/app/main.py:53-76`；根因 `engine/observability/summary_store.py:257-261`（retention 不删 `runs/<id>.json` 与 `.jsonl.head`）
- **机制**：启动对账无法区分"被修剪"与"崩溃窗口"，把每个被修剪 run 重新 finalize 成 `finished_at=now` 的空事件僵尸 summary → 按 `finished_at DESC` 排最前 → 把真实 run 的 summary+trace 挤出保留窗 → 被挤者下次重启又复活……反驳者脚本实证（max_runs=2、3 个 run）：**一次对账循环内级联删光全部真实 trace，且永不收敛**。比原报告更严重。
- **补丁**：(a) `_apply_retention` 连带删 `runs/<id>.json`、`.jsonl.head`/`.torn`，使"终态 state + 无 summary"重新成为可靠崩溃信号；(b) `_save_recovery_summary` 用 `state.updated_at` 而非 `_now()` 作 finished_at，消除时间戳反转放大器。两处都做。

### 11. 取消落在记忆收尾窗口 → RUN_FINISHED 永不落库 → 暂停链被破坏性重启并覆盖 checkpoint
- **位置**：`engine/execution/orchestration/lifecycle.py:727-729, 783-798`
- **机制**：AWAITING_INPUT 暂停后仍要过 `_persist_runtime_learning`（≤30s，含 LLM 调用），server 的 `done` 要等生成器耗尽——窗口内任何断连（Esc/关终端/断网）→ CancelledError 在 783 行重抛，786 行终态记录被跳过（not-drained 分支有 `asyncio.shield`，drained 分支没有）→ run 永久 RUNNING。下一条消息被劫持进 pipeline，`_checkpoint_owner_still_running` 见 owner 存活 → 从节点 0 重跑，第一次暂停即 `os.replace` 覆盖原 checkpoint——原链全部已提交输出永久丢失。
- **补丁**：drained 分支在进入记忆收尾**之前**（或 shield 包住）先写终态 transition/RUN_FINISHED——记忆收尾本就是终态后的簿记；或让 `_checkpoint_owner_still_running` 对 `awaiting_user_input=True` 的 checkpoint 放行。

## P2（边界条件触发或影响较轻）

### 12. compaction 单向闩锁：一次瞬时失败整 run 降级为删除式裁剪
`react_loop.py:526-530`。压缩 LLM 一次超时/摘要一次被截 → `model_compaction_enabled=False` 无恢复路径。**补丁**：瞬时 `compaction_failed` 允许有限重试/冷却恢复；`compaction_rejected` 按原因区分，TRUNCATED 缩窗重试一次。

### 13. 混合流式/非流式分片被整体标 `already_streamed`，前段实时视图永久丢失
`react_loop.py:773, 847`。首 delta 前断流→非流式回退（未渲染）→length 续写→第二段流式成功→整段标已渲染，前段只落库不上屏。**补丁**：`final_text_parts` 逐分片记录 `(text, was_streamed)`，未流式分片单独发不带标志的 TEXT_DELTA。

### 14. Dream cleanup journal 整文件哈希校验：崩溃后一旦有追加，恢复永久失败、sanitize/回收全停
`dream.py:315-344`。替换后清 journal 前被硬杀 → 重启后任何入账追加使 hash 三不匹配 → 每次 Dream 提前 return，journal 无清除路径（只能手删 `.dream_cleanup.json`），游标错位静默跳证据。**补丁**：journal 存 `remaining_text` 的字节长度 + 前缀哈希（old 侧同理），恢复时按前缀判定修剪是否已发生，容忍尾部追加。

### 15. run 生命周期工件三类无界增长 + 启动全量扫描
`run_state.py`（无删除 API）、`tool/ledger.py`（无 DELETE）、`.jsonl.head` 孤儿。一年重度使用 ≈ 7 万 state JSON + 数百 MB sqlite，启动两次全量 glob+parse。是 #10 复活循环的使能条件。**补丁**：retention 修剪时连带删 state/head/torn + `DELETE FROM tool_executions WHERE run_id=?`。

### 16. 每个普通事件交付前串行 3 次 fsync（run-state 2 + trace 链 1）
`lifecycle.py:672-673`、`run_state.py:636-642`、`hash_chain.py:163-224`。40 迭代工具轮 ≈ 700-1200 次 fsync 插在 SSE 交付路径上，磁盘忙时单轮多阻塞 10 秒级。**补丁**：THINKING/TOKEN_USAGE/CONTEXT_USAGE/TOOL_CALL_* 改内存态+合并延迟写、trace 侧 sync=False（终态 seal 统一 fsync）；RUN_STARTED/FINISHED/approval/SKILL 事件保持现状同步落盘。

### 17. LLMGate 把门禁 LLM 基建故障折算成内容失败：白烧 2 次整节点重跑后 blocked + 清全链 checkpoint
`gate.py:122-127, 167-176`。仅用户自建管线声明 LLM 门禁时触发。**补丁**：infra 故障返回专用标记/异常，pipeline 对其不计 FailureLoopGuard、不重跑、保留 checkpoint，按 failed(provider outage) 收尾——对齐 memory 管线 rejected/failed 语义。

### 18. SessionCheckpoint 只存节点下标不存节点身份：暂停期间改 pipeline YAML 后错位恢复
`checkpoint.py:27`、`agent_loop.py:231`。**补丁**：checkpoint 增加 `node_skill` 字段，恢复前比对 `chain.nodes[idx].skill_name`，不符走既有 stale-clear 分支。

### 19. RecordingLLM 每轮全量读改写：O(N²) 阻塞 IO + 三路由交错写坏回放顺序
`replay.py:198-205`；`engine_runtime.py:73` 三个路由共用同一录制路径。仅 opt-in 录制时触发。**补丁**：`O_APPEND` 单写追加（load_recording 已容忍尾部截断行）；录制文件按 route 分流。

### 20. cost-tracker 死通路：lifecycle 传的 session_stats 恒为空
`lifecycle.py:761-767` 硬编码 `"session_stats": {}` → hook 恒短路，`metrics/costs.jsonl` 从建仓起零写入，CLAUDE.md §6a 的宣称不实。**补丁**：收尾时把本 run 聚合 token_usage + model 填进 session_stats；补端到端断言 costs.jsonl 有记录。

---

## 证伪（不要当缺陷修）

- **CJK 按 3 token/字符估算**（`budget.py:67-68`）：注释块专门论证过的 BPE 字节回退上界，`test_fitting.py:47` 精确锁定 `estimate_tokens("一") == 3`，`test_compression.py:54` 注释背书"必须在 provider 拒绝前提前压缩"。是保守设计不是缺陷；按 provider 校准属产品改进，另行立项。
- **memory_ops 并发追加竞态**（`memory_ops.py:211` 无锁 append）：该工具 `hidden:True` + 出厂白名单不含 + `enabled_tools_from_config` 过滤 hidden（测试锁定），模型三层都调不到，场景走不通。若日后放出白名单会立即成为真实竞态——代码处值得留一句注释。

## 建议打补丁顺序（按聚簇，每簇一个 PR）

| 批次 | 条目 | 聚簇理由 |
|---|---|---|
| 1 | #1 #2 #12（#13 顺手） | 同在 react_loop/context 压缩链路，上下文正确性最高杠杆 |
| 2 | #4 #5 #6 | 同在 memory compile/guards/review，记忆演化三连环 |
| 3 | #10 #15（+#11） | 同根因：retention/终态记录不完整；#11 同文件 lifecycle |
| 4 | #8 #9 #3 | 预算与流边界：usage 兜底、SSE 上限、审批熔断 |
| 5 | #14 #16 #17 #18 #19 #20 | 独立小补丁，可拆可并 |
