# 04 · Engine 设计与实现

> **定位**：本文是 Agent-Smith 自研 Agent 框架的**设计规格书**——完整描述概念模型、每个子系统的设计、接口约定、数据流与扩展点。
>
> 与其他文档的分工：
> - `03-Agent执行引擎.md` —— 执行引擎的实现讲解
> - `10-自研Agent框架深度解析.md` —— 设计理念 WHY + 框架对比 + 面试问答
> - **本文（11）** —— 设计规格：是什么、怎么设计的、约定是什么、如何扩展
>
> 所有描述均以当前代码为准（引用形如 `engine/execution/orchestration/agent_loop.py`）。

---

## 目录

1. [设计目标与核心哲学](#一设计目标与核心哲学)
2. [概念模型](#二概念模型)
3. [总体架构与一次对话的生命周期](#三总体架构与一次对话的生命周期)
4. [Agent 身份设计（人格层）](#四 Agent 身份设计人格层)
5. [System Prompt 组装设计](#五system-prompt-组装设计)
6. [执行引擎设计](#六执行引擎设计)
7. [记忆系统设计](#七记忆系统设计)
8. [工具系统设计](#八工具系统设计)
9. [技能系统设计](#九技能系统设计)
10. [插件系统设计](#十插件系统设计)
11. [安全设计](#十一安全设计)
12. [配置体系](#十二配置体系)
13. [服务端集成契约](#十三服务端集成契约)
14. [设计权衡与已知局限](#十四设计权衡与已知局限)
15. [附录：文件地图](#附录文件地图)

---

## 一、设计目标与核心哲学

### 1.1 产品命题

Agent-Smith 交付的不是"聊天机器人"，而是**本地 Agent**：

- **有身份**：Smith 有明确的角色、原则、风格、工作流，而不是一个无边界聊天框
- **有过程**：复杂任务走强制的技能链（规划 → 测试策略 → 验证 → 评审），每步有质量门禁
- **有成长**：记忆会积累、编译、遗忘；偏好会被自动学习；技能可以自装自改
- **有边界**：破坏性操作被规则层拦截，产出必须带证据才能通过门禁
- **有本地执行力**：它是 terminal-native / shell-native 的本地进程 Agent，`shell` 是一等执行能力，结构化工具是对 shell/file/git/web 常见操作的安全封装

### 1.2 核心哲学：宏观确定性，微观自由度

框架的所有设计都源自一个判断：**LLM 有创造力但不可靠，代码可靠但没创造力**。因此把两者各放在擅长的位置：

| 由代码控制（确定性） | 由 LLM 控制（自由度） |
|---|---|
| 任务路由（走哪条链） | 理解需求、拆解问题 |
| 技能链顺序（先规划后编码） | 每一步内怎么思考 |
| 质量门禁（产出必须有证据） | 选什么工具、传什么参数 |
| 失败回退（退到哪个节点） | 怎么组织回复 |
| 安全拦截（什么命令不能跑） | 换什么方案绕开失败 |

对应到代码就是**四层嵌套执行模型**（见第六章）：

```
任务路由 (task_router)          ← 规则决定走哪条路
  └─ 技能链 DAG (skill_chain)    ← 代码决定步骤顺序 / 门禁 / 回退
       └─ ReAct 循环 (agent_loop) ← LLM 在单步内自由思考-用工具-观察
            └─ 安全护栏 (tool_guard) ← 每次工具调用前的规则拦截
```

### 1.3 三条不可违背的架构约束

1. **依赖单向**：`app → server → engine → common`，engine 不知道 HTTP，common 不知道业务
2. **内容与代码分离**：新增技能、工具、知识只改 `agents/` 下的 md/yaml/json/Python 内容资产，不碰执行内核
3. **Smith 身份种子只读**：`agents/smith/` 由 Git 跟踪、永不被运行时修改；运行时读写的是 `~/.agent-smith/` 下的副本

---

## 二、概念模型

### 2.1 实体一览

| 概念 | 是什么 | 存在哪 | 生命周期 |
|---|---|---|---|
| **Smith Profile Seed（身份种子）** | Smith 身份的出厂定义（6 个文件） | `agents/smith/` | Git 跟踪，只读 |
| **Agent Profile（Smith 档案）** | Smith 的运行时身份、配置、技能 | `~/.agent-smith/agent/` | 初始化后随使用成长 |
| **Identity（领域身份）** | 一次任务的领域 prompt、能力边界与路由 | `agents/identities/*.yaml` | 启动扫描、会话固定 |
| **Session（会话）** | 用户与 Smith 的一次对话上下文 | SQLite + Agent Profile 下的会话状态 | 用户创建/归档 |
| **Task（任务）** | 一条用户消息经路由后的执行单元 | 内存中（RouteDecision） | 单次执行 |
| **Skill（技能）** | 一段"怎么做某类事"的方法论（SKILL.md） | 内置 `agents/skills/` / Smith 自装 `~/.agent-smith/agent/skills/` | 内置只读；自装可编辑、带版本 |
| **Tool（工具）** | 一个可被 LLM 调用的原子能力（Python 函数） | `agents/tools/*.py` + MCP 外接 | 启动时注册 |
| **Plugin（插件）** | 外部事件源 + 处理器（如 GitHub、日报） | `agents/plugins/<name>/` | 服务启动时发现、触发器常驻 |
| **Memory（记忆）** | Smith 的证据与三份正式记忆视图 | `~/.agent-smith/agent/memory/` + `context.md` | 单 Smith 共享，持续写入、审核式编译与遗忘 |
| **Gate（门禁）** | 一段产出的质量检查器 | `engine/execution/pipeline/gate.py` | 每个技能链节点绑定一个 |
| **SkillChain（技能链）** | 带门禁和回退边的技能 DAG | `agents/pipelines/*.yaml` | 由身份路由引用 |

### 2.2 实体关系

```
Template ──(初始化时复制)──▶ Agent Profile(Smith) ──(1:N)──▶ Session ──(1:N)──▶ Message
                                      │
                    ┌─────────────────┼────────────────┬──────────────┐
                    ▼                 ▼                ▼              ▼
                 Memory            Skills(自装)      config.yaml    context.md
              (证据+编译+遗忘)     (版本化可编辑)    (Smith级覆盖)   (审核后的用户协作记忆)

用户消息 ──catalog route──▶ RouteDecision(identity/route/pipeline) ──▶ SkillChain 或 DIRECT
                                    │
                             SkillNode × N ──每节点──▶ Skill + Gate (+ 回退边)
                                    │
                              ReAct 循环 ──▶ Tool 调用 ──ToolGuard──▶ 执行
```

### 2.3 数据目录（运行时的"Agent 档案柜"）

```
~/.agent-smith/
├── config.yaml                     # 平台级配置（LLM key/url/model）
├── agent/                          # Smith 的唯一运行时档案（目标）
│   ├── role.md / style.md / workflow.md / toolbox.md   # 人格（从模板复制）
│   ├── context.md                  # 结构化用户偏好与协作共识（Compiler + Reviewer）
│   ├── config.yaml                 # Agent 级 LLM 覆盖 / mcp_servers / tools.enabled
│   ├── memory/
│   │   ├── recent.jsonl            # 已清洗的追加式证据日志
│   │   ├── recent.md / durable.md  # 审核后的近期与长期项目记忆
│   │   ├── memory_history.jsonl    # 编译、审核与写入审计
│   │   ├── episodes/*.md           # 主题摘要（按需检索）
│   │   ├── episodes/search.sqlite  # 可丢弃的 FTS5 索引
│   │   ├── .fp_context / .fp_recent / .fp_durable
│   │   ├── .compile_offset / .durable_offset
│   │   ├── .compile_counter        # 编译触发计数（普通事件每 5 turn）
│   │   └── .dream_counter          # Dream 触发计数（每 50 turn）
│   ├── skills/<name>/SKILL.md      # 自装技能（.versions/ 下保留 10 个历史版本）
│   ├── sessions/.state/<sid>.json  # 执行断点（checkpoint）
│   └── .learner_state.json         # 偏好学习置信度计数
└── sqlite/agent-smith.sqlite       # 索引库（Agent/会话/消息，WAL）
```

声明式 identity 只负责路由，不拆分 Smith 的记忆；可变状态直接位于唯一 profile 的 `memory/` 下。

---

## 三、总体架构与一次对话的生命周期

### 3.1 核心分层架构

```
┌─────────────────────────────────────────────────────┐
│ shell/      Ink 终端前端（HTTP + SSE）               │
├─────────────────────────────────────────────────────┤
│ server/     FastAPI 平台层                           │
│   routers(薄壳) → services(编排) → infrastructure(仓储) │
├─────────────────────────────────────────────────────┤
│ engine/     自研 Agent 框架（本文主体）               │
│   execution │ prompt │ memory │ llm │ tool │ skill │ plugin │ safety │
├─────────────────────────────────────────────────────┤
│ agents/     纯内容层：模板/技能/工具/插件/安全规则     │
├─────────────────────────────────────────────────────┤
│ common/     配置合并 / SQLite / 文件系统 / 日志       │
└─────────────────────────────────────────────────────┘
```

engine 内部模块与依赖：

```
server composition ──▶ execution + observability
execution ──▶ llm, context, skill, tool, safety, memory, mcp
observability ──▶ execution.events（实现 execution 定义的观察者协议）
skill ──▶ llm/tool/safety 的窄接口（ReAct 实现由 execution 注入）
tool / mcp / memory / safety ──▶ 不反向依赖 execution 实现
```

### 3.2 一次对话的完整生命周期（流式）

入口：`server/app/services/session_service.py` → `engine.execution` 公共接口 → `engine/execution/orchestration/lifecycle.py`

```
1. app 发 POST /sessions/{id}/messages/stream
2. SessionService 校验会话、落库用户消息
3. engine.reply_stream(agent_id, name, message):
   a. resolve_llm_config() 五级合并配置 → build_llm_client()
   b. ToolRegistry.load_providers(agents/tools/)     ← 扫描注册内置工具
   c. 读 Agent config.yaml 的 mcp_servers → MCPClient 连接 STDIO / Streamable HTTP → 注册 mcp_* 工具
   d. SkillRegistry.load_builtin() + load_agent_skills()  # 语义是加载 Smith 自装技能
   e. PromptAssembler.assemble_detailed()            ← 带来源契约与审计清单的 system prompt
   f. route_task(message) → DIRECT / FEATURE / BUGFIX
   g. 构建 SkillChain（或 None）、FailureLoopGuard、ToolGuard
4. run_agent_stream() 产出 ExecutionEvent 流：
   ROUTE_DECIDED → (SKILL_START → 工具事件… → GATE_RESULT → SKILL_END)* → TEXT_DELTA → DONE
5. reply_stream 把事件翻译成文本块 yield 给 SessionService
6. SessionService 逐块下发 SSE，结束后整条落库
7. finally: 提取学习信号并通过 memory lifecycle hook 写证据；llm.close()
```

流式和非流式路径共享同一收尾：`save_conversation_memory()` 写证据；普通事件每 5 turn 触发三视图编译，明确学习信号立即触发；每 50 turn 触发 Dream。

---

## 四、Agent 身份设计（人格层）

### 4.1 设计原则：文件即人格

Smith 的基础人格 = 一个目录下的 6 个文本/配置文件。Agent-Smith 不再通过新增模板目录扩展身份；具体能力通过 `agents/skills/*/SKILL.md` 扩展。这样做的理由：

- 人格是内容问题不是代码问题——产品/运营可以直接改
- Git 天然提供人格的版本管理和 review 流程
- Prompt 组装器按固定顺序读 Smith 基础档案，场景 SOP 由 skill 按需加载

### 4.2 模板文件规格

| 文件 | 角色 | 内容约定 | 示例 |
|---|---|---|---|
| `role.md` | **身份**（Smith 是谁） | Core Mission / Non-Negotiable Principles / Done Criteria / Anti-Goals 四段 | Smith 是唯一常驻 Agent；能力通过 skill 扩展 |
| `style.md` | **风格**（我怎么说话） | 沟通语气、汇报格式 | — |
| `workflow.md` | **全局工作规约** | 路由原则、工具调用规约、skill 使用规约 | 仅按需使用当前已加载的 skill |
| `toolbox.md` | **工具观** | 工具使用原则；运行时追加实际注册的工具清单 | — |
| `context.md` | **用户上下文** | 已确认偏好、协作模式、稳定用户背景 | 由 Compiler 生成、Reviewer 审核 |
| `config.yaml` | **配置** | name/role/description、LLM 覆盖、`tools.enabled` 白名单、`mcp_servers` | `model: null` 表示继承上层 |

能力清单、风格标签和交付管线不再放在 Smith 模板 JSON 中；这些内容下沉到 skill metadata 和 `SKILL.md`。

### 4.3 Agent 生命周期

```
初始化：agents/smith/ ──复制──▶ ~/.agent-smith/agent/
运行：只读写副本；模板永不被触碰
成长：memory/ 积累证据并编译；skills/ 自装；context.md 由统一记忆闭环更新
删除：删目录 + SQLite 记录
```

模板升级不影响已创建 Agent（各自独立演化）；这是有意为之——Agent 的"成长痕迹"比模板一致性更重要。

---

## 五、System Prompt 组装设计

实现：`engine/context/assembler.py`（`PromptAssembler.assemble_detailed`）

### 5.1 统一来源契约与分层结构

每个 Layer 统一携带 `source`、`source_ref`、`scope`、`authority`、`trust`、`load_reason` 和 `trim_priority`。渲染进模型的每个非空段均有同一格式的可见标签：

```text
## Context: {display_name}
[Source: {source_ref} · Authority: {authority} · Trust: {trust} · Scope: {scope} · Load: {load_reason}]
```

因此模型能区分“来自哪里”和“能否覆盖谁”；系统也不再把多个不同来源预先拼成一个不可审计的大段。按固定顺序拼接（`\n\n---\n\n` 分隔），空段不渲染：

| # | 段 | 来源与作用域 | 稳定性 | 被裁剪优先级* |
|---|---|---|---|---|
| 0 | 身份 role | `role.md` | 稳定 | **永不裁剪** |
| 1 | 风格 style | `style.md` | 稳定 | 9（最后裁） |
| 2 | 工作流 workflow | `workflow.md` | 稳定 | **永不裁剪** |
| 3 | 工具策略 / 工具定义 | `toolbox.md` / ToolRegistry（两层） | 稳定 | 8 / 7 |
| 4 | 技能清单 | SkillRegistry 摘要 | 稳定 | 6 |
| 5 | 学习后的用户上下文 | `context.md`，用户范围的参考材料 | 动态 | **永不裁剪** |
| 6 | 全局指令 / 项目指令 | `global:SMITH.md`（用户）/ `project:.smith/SMITH.md`（项目），两层 | 动态 | **永不裁剪** |
| 7 | Identity / eval 指导 | Identity Catalog / engine，分层 | 每次不同 | **永不裁剪** |
| 8 | 输出风格 | `agents/output_style.md` | 稳定 | 1（最先裁） |
| 9 | 记忆治理 | engine 固定合同：Todo/plan/task 不可作为持久记忆 | 稳定 | **永不裁剪** |
| 10 | recent / durable / episodes | `memory:recent.md` 常驻；durable 与 episodes 查询时分别召回 | 动态 | 2 / 3 / 4 |
| 11 | 运行时上下文 / 运行控制 | runtime facts / `execution/runtime_control.py`，分层 | 每次不同 | **永不裁剪** |

> \* 当前裁剪顺序为输出风格 → episodes → durable → recent → skills → tools → style。role、workflow、context、`SMITH.md`、运行时指导、记忆治理、运行时上下文与运行控制合同不裁剪。

### 5.2 稳定层缓存与 Prefix Cache

- 段 0–4（身份/风格/工作流/工具/技能）对同一 Agent 几乎不变 → 取 MD5 作为**稳定前缀 hash**（`assembler.py:126`）
- `PromptAssembler.get_prefix_cache_key()` 暴露该 hash；`LLMClient.chat(prefix_cache_key=...)` 通过 `extra_body.prefix_cache_key` 传给支持前缀缓存的推理服务（`llm/client.py:56`）
- 设计意图：把"稳定在前、动态在后"的分层顺序转化为真实的推理成本节省

### 5.3 PromptManifest：逐层可审计但不落原文

`assemble_detailed()` 同时返回 provider prompt 和 `PromptManifest`。每层清单记录逻辑 id、来源/引用、范围、权威度、信任级别、加载原因、内容 SHA-256、字符数、估算 token 与动作（`loaded` / `trimmed` / `empty`）；**不记录 prompt 原文、绝对路径或用户内容**。

`RunEventRecorder` 将该清单以 `prompt_manifest` 写入 owner-only 的 `traces/{run_id}.jsonl`。这使一次请求可逐层解释“为什么被加载、是否被裁剪”，又不会把模型上下文复制成另一份敏感日志。没有 Team memory 层；未来若接入新来源，必须映射为同一 `PromptLayer` 契约，而不是旁路拼接字符串。

### 5.4 记忆注入策略

```
固定注入：assemble_memory(include_durable=False) 只装配 recent.md，标记为 memory_recent
按需注入：retrieve_relevant_memory() 分别产生 memory_durable 与 memory_episodes；它们不会再合并后失去来源
安全围栏：context 和项目记忆分别标注为历史参考，不能覆盖当前请求、SMITH.md 或系统规则
```

编译产物是经过 Reviewer 审核的完整结构化 Markdown（context ≤4K、recent ≤8K、durable ≤10K）；durable 不再整份常驻，召回块另有 4K 上限。

### 5.5 引擎运行控制合同

`engine/execution/runtime_control.py` 把不可替换的运行标准投影为 system prompt 的最后一层。它不是 `workflow.md` 的替代品：workflow、identity 与 skill 仍可定义领域 SOP；控制合同只定义任何任务都成立的底线。

- **执行边界**：`ToolPolicy` / `ToolGuard` 的裁决不可绕过；模型不得把被阻断或待审批的操作描述为已完成。
- **失败行为**：工具被阻断、连续失败、输出截断与“只承诺下一步”的假完成，均由 ReAct 追加 engine 生成的 system 指令来纠偏。
- **交付责任**：发生工具调用后，最终回复须覆盖结果、证据或改动、失败或遗漏；未完成时才给出下一步。

合同只使用 engine 生成的固定文本，不拼接用户消息、项目 `SMITH.md`、记忆或工具输出。它位于可插拔内容之后以明确优先级，但真正的安全边界仍由代码执行，不依赖模型遵守提示词。

---

## 六、执行引擎设计

### 6.1 分层执行模型总览

```
用户消息
   │
   ▼
┌──────────── 第一层：任务路由 task_router.py ────────────┐
│ [bugfix]/[feature]/[direct] 用户覆写 > 关键词计分 > LLM 兜底 │
└──────┬──────────────────────────┬───────────────────────┘
   DIRECT                    FEATURE / BUGFIX
       │                          │
       ▼                          ▼
  纯 ReAct 循环          ┌─ 第二层：技能链 DAG skill_chain.py ─┐
                        │  节点 = 技能 + 门禁 + 可选条件        │
                        │  每节点内部 ↓                        │
                        │  ┌─ 第三层：ReAct 循环 agent_loop.py ┐│
                        │  │  think → tool → observe（≤20 轮） ││
                        │  │  每次工具调用 ↓                   ││
                        │  │  ┌ 第四层：ToolGuard 安全拦截 ┐   ││
                        │  └──┴───────────────────────────┴───┘│
                        │  产出 → RubricGate → 节点 Gate        │
                        │  失败 → FailureLoopGuard → 重试/回退/阻断 │
                        └──────────────────────────────────────┘
```

### 6.2 任务路由（task_router.py）

三级决策，**优先级：用户覆写 > 关键词 > LLM 兜底**：

1. **覆写**：消息以 `[bugfix]` / `[feature]` / `[direct]` 开头 → 直接采纳并剥离标签（`_check_override`）
2. **关键词计分**：中英双语关键词各一组（bugfix 17 个、feature 16 个），分数高者胜，平手或零命中 → DIRECT
3. **LLM 兜底**（`route_task_with_llm`）：仅当关键词判 DIRECT 且传入了 llm 时，用一次分类调用复核

设计取舍：路由错误的代价不对称——DIRECT 误入技能链只是变慢，FEATURE 误判 DIRECT 会跳过质量流程。关键词表刻意宽泛（"修改""添加"都会命中），宁可多进链。

### 6.3 技能链 DAG（skill_chain.py）

**结构**：

```python
SkillNode(skill_name, gate, condition=None)   # condition(ctx) 返回 False 则跳过
SkillChain(nodes, backtrack_map)              # backtrack_map: 失败节点 → 回退目标
```

**预置链**（QoderWake 顺序）：

| 链 | 节点序列 | 回退边 |
|---|---|---|
| feature | planning → architecture（条件：计划涉及 ≥3 个文件） → testing-strategy → change-validation → code-review | change-validation→planning；code-review→change-validation；testing-strategy→planning |
| bugfix | sde-debug → planning → testing-strategy → change-validation → code-review | 同上 |
| refactor | planning → testing-strategy → change-validation → code-review | 同上 |

**配置即代码**：`SkillChain.from_workflow_md()`（`skill_chain.py:93`）能直接解析 Agent `workflow.md` 中的链定义：

```
### Feature 路由
    planning → architecture(仅大型变更) → testing-strategy → change-validation → code-review
```

- `→` 分隔的技能名映射到 `_GATE_MAP` 里的门禁（未知技能 → 默认 SkillRubricGate）
- 括号里含"仅/only/条件"→ 生成条件节点

这意味着**每个 Agent 可以在自己的 workflow.md 里声明专属技能链**，引擎读得懂——人格文件与执行引擎在这里闭环。

**回退边设计**：只允许"验证类节点退回生产类节点"（验证失败说明计划/实现有问题），不允许任意跳转——把回退空间限制成可推理的。

### 6.4 ReAct 微观循环（agent_loop.py）

一个循环三个变体，共享同一逻辑骨架：

| 变体 | 返回 | 用途 |
|---|---|---|
| `_react_loop`（:31） | `str` | 技能链节点内部、非流式 reply |
| `_react_stream_loop`（:103） | 文本 chunk 流 | 纯文字流式（终答改用 `chat_stream` 重发一次以获得逐字输出） |
| `_react_event_loop`（:161） | `ExecutionEvent` 流 | SSE 结构化事件（工具调用进度可见） |

骨架内的三个保护机制：

1. **滑动窗口**：对话超过 40 条 → 保留 system + 最近 30 条（防上下文爆炸）
2. **迭代上限**：默认 20 轮，超限返回 "Max ReAct iterations reached"
3. **连续失败自适应**：工具连续失败/被拦截 3 次 → 注入 system 提示"换方法：换工具、简化命令、或不用工具直接解释"，计数清零。**拦截不终止**——被 ToolGuard 挡下的调用以 `[BLOCKED] 原因` 作为工具观察值回给 LLM，让它自己绕路

### 6.5 门禁体系（gate.py，三级质量检查）

**统一接口**：`Gate.check(output, context) -> GateResult(verdict: pass|fail|retry, reason, retry_hint)`

每个技能链节点的产出要过两道检查，其中第二道可再叠加 LLM 复核：

```
产出 ──▶ ① SkillRubricGate（通用体检，所有节点共享）
              长度>50 且 含代码块/文件引用 且 无错误标记
        ──▶ ② 节点专属 Gate（规则正则）
              ──▶ ③ LLMGate（可选语义复核，仅 planning / change-validation）
```

**13 个门禁一览**：

| Gate | 绑定技能 | 检查什么（本质：要证据，不要口号） |
|---|---|---|
| PlanningGate | planning | ≥3 个编号步骤 + 含验证点关键词 |
| DesignGate | architecture | 涉及文件 + 数据流 + 依赖三要素 |
| TestGate | testing-strategy | ≥2 个测试用例 + 边界/异常覆盖 |
| ValidationGate | change-validation | 有执行痕迹 + 有具体 pass/fail 数字（不接受一句"passed"） |
| ReviewGate | code-review | 有严重度分级（P0/P1/P2）+ 有发现或明确"无问题" |
| RootCauseGate | sde-debug | 有根因陈述 + 有证据（日志/堆栈/输出） |
| SkillRubricGate | （通用体检） | 长度>50 且含代码块/文件引用且无错误标记 |
| LLMGate | planning / change-validation | 正则先跑，pass 后再花一次 LLM 语义复核 |
| TestDeliveryGate | （交付场景） | 测试真的跑了 + 有 pass/fail 计数 |
| UnderstandingGate | understand | 复述需求 + 识别边界条件/约束/假设 |
| ContractAlignmentGate | （实现前对齐） | 实现方案逐项对照计划，有一致/偏差判定 |
| GitWorktreeGate | （git 场景） | 工作树干净无冲突 |
| PRGate | （git 场景） | 规范提交且无敏感文件入库 |

**LLMGate 的双层设计**（`gate.py:393`）：正则先跑（快、免费、抓形式）；正则 fail 直接返回；正则 pass 后才花一次 LLM 调用做语义复核（"这个计划是不是模板套话？"）。LLM 调用失败时静默信任正则——门禁永远不能因为自身故障阻塞执行。

**verdict 语义的实际处理**：主循环只区分 `pass` 与非 pass——`fail` 和 `retry` 都进入 FailureLoopGuard 裁决（`agent_loop.py:455-467`）。`retry_hint` 会作为下一次尝试的追加提示注入。

### 6.6 失败处理与回退

三层递进，全部是**代码裁决，不问 LLM**：

**第一层：Rubric 重试**（节点内，最多 3 次，`agent_loop.py:412`）
- 第 2 次尝试：注入上次的 `retry_hint`
- 第 3 次尝试：注入"换一种完全不同的策略"
- 3 次全败 → 直接 Blocked（产出连基本体检都过不了，不值得走回退）

**第二层：FailureLoopGuard**（跨节点状态机，`backtrack.py:13`）

失败签名 = `技能名 + 产出MD5前8位`。语义：**同样的地方摔同样的跤才算重复失败**。

```
record(sig) 裁决规则（max_same=2, max_strategies=2）：
  同一签名计数 < 2      → retry   （原地再试）
  已在 ≥2 个技能上失败过 → blocked （多点失败，停下来交给人）
  否则                  → switch  （按 backtrack_map 回退）
```

**第三层：回退护栏**
- `switch` 且该节点有回退边 → 跳回目标节点索引，全链回退次数 `max_backtracks=5` 封顶
- 无回退边或超限 → Blocked

**Blocked 的设计含义**：不是异常，是**正常出口**——框架承认"我搞不定"并带着最后的门禁原因停下，好过无限重试烧 token 或硬着头皮产出劣质结果。

### 6.7 断点恢复（checkpoint.py）

- 流式执行中，每个技能节点通过门禁后写一次 `SessionCheckpoint`（agent_id / session_id / task_type / 当前节点索引 / 累积 context）到 `sessions/.state/<sid>.json`
- 链完成后清除；崩溃则文件残留，`restore()` / `list_active()` 可枚举待恢复会话
- session_id 有正则白名单 + resolve 校验，防路径穿越（`checkpoint.py:39`）

### 6.8 事件协议与本地 Trace（observability/）

引擎与前端之间的流式契约由 `engine/execution/events.py` 定义。每种 `EventType` 带自由 dict 负载；传输层负责将它格式化为 SSE：

```
route_decided → skill_start → tool_call_start/tool_call_result …
→ gate_result → skill_end | backtrack | blocked → text_delta → done
```

设计点：**引擎产出结构化事件，翻译和留存由观测层处理**。执行层仅通过 `RunObservation` 写入事件与 prompt manifest；它内部以 best-effort 方式写入 owner-only 的 JSONL trace，在终态以原子文件保存不含原始 payload 的 `RunSummary`，并将事件投影给执行控制面的 `RunStateStore`。服务层仅通过 `ObservabilityReader` 查询摘要、trace、`Run Incident`、结构化 RCA、跨 Run 的健康度和改进提案；IncidentDetector 根据摘要和脱敏 trace 归类预算耗尽、运行失败、重复回退及工具超时等可行动信号，RunDiagnoser 则返回失败节点、证据和保守建议，HealthCalculator 汇总成功率、工具成功率、回退和 token 成本，ImprovementProposer 只生成必须经 approval gate 批准的建议，绝不自动改配置。`reply_stream` 把事件翻成文本标记（`[⚙ planning]`、`[门禁: pass]`、`[↩ 回退: …]`）供纯文本客户端；SSE 端点可以原样透传给富客户端渲染进度树。

---

## 七、记忆系统设计

设计目标：**越用越懂你，同时让记忆可审核、可回退、按需召回**。完整规格见 [`docs/05-Engine-记忆系统.md`](05-Engine-记忆系统.md)，唯一内容规则见 [`engine/memory/MEMORY_POLICY.md`](../engine/memory/MEMORY_POLICY.md)。

### 7.1 主链

```text
对话/工具结果
  → recent.jsonl 证据
  → Compiler 按 MemoryPolicy 生成 context/recent/durable 草稿
  → Reviewer 按同一规则审核，最多反馈重试三轮
  → 结构、安全、预算检查
  → 备份 + 原子替换 + memory_history.jsonl 审计
```

无 Reviewer、审核拒绝、超时或结构错误都不会替换旧文件。代码不再提供自由文本降级写入。

### 7.2 写入与学习

- 工具/技能活动写 `work` 证据；
- 无工具纯聊天中的明确偏好、纠正、决定、记住和忘记也写证据并立即编译；
- `UserPreferenceLearner` 的稳定模式需要观察 3 次，只产出信号，不直接写 Markdown；
- 普通一次性闲聊不记录；
- `SMITH.md` 是用户规则，自动学习永不修改。

### 7.3 Compile、Dream 与召回

- context：用户协作记忆，常驻且不参与 token 裁剪；
- recent：3 天滚动窗口，必要时回退到 7 天，窗口空时清除；
- durable：按 `.durable_offset` 增量合并，回答时只召回与当前问题匹配的条目；
- episodes：继续使用 FTS5 按需召回；
- Dream：每 50 turn 做清秘、去重和压缩，整理结果也必须经过 Reviewer。

所有正式 Markdown 的固定章节、准入条件、冲突优先级和字符预算只定义在 MemoryPolicy，不散落到模板文件中。

---

## 八、工具系统设计

### 8.0 工具定位：shell-native，但不是裸 shell

Agent-Smith 的工具系统按 **本地终端 Agent** 设计：ReAct 循环里的 LLM 可以选择工具、传参、观察结果，其中 `shell` 是主执行通道。`read_file`、`write_file`、`grep`、`git_ops`、`web_fetch` 等工具不是替代 shell 的另一套权限体系，而是把高频操作结构化，让 UI、日志、guard 和错误处理更稳定。

因此工具系统的设计边界是：

- **能力优先于工具名**：安全策略按文件访问、网络访问、Git 副作用、写入、删除、secret 访问等能力建模，而不是只看调用的是不是 `shell`。
- **结构化工具优先复用同一 guard**：能枚举文件、读取内容、修改文件或移除 worktree 的工具，都必须进入文件边界检查；能访问网络的工具必须进入网络边界检查。
- **shell 放行不等于无审计**：本地任务默认可执行，但敏感路径、破坏性命令、内网地址、密钥文件、强制 push 等高风险行为要被拦截或要求用户显式确认。
- **错误作为观察返回**：工具失败不应炸掉 Agent 主循环，而应作为 `ToolResult(is_error=True)` 返回，让 LLM 换策略或向用户报告。

### 8.1 Provider 约定：一个文件一个工具，零框架依赖

`agents/tools/*.py` 每个文件只需两个顶层符号：

```python
TOOL_META = {"name": ..., "description": ..., "parameters": {JSON Schema}}
async def execute(**kwargs) -> str: ...   # 同步函数也支持
```

`ToolRegistry.load_providers()`（`tool/registry.py:26`）用 `importlib` 逐文件加载注册。工具作者不 import 任何 engine 代码——**约定优于依赖**，这是"agents/ 零代码依赖"分层规则的落地。

### 8.2 内置工具清单

| 工具 | 职责 |
|---|---|
| `read_file` / `write_file` / `edit_file` | 文件读写和精确替换（受 FileGuard 目录白名单约束） |
| `grep` / `glob_files` / `list_dir` | 文件搜索与目录枚举（同样受 FileGuard 约束） |
| `shell` | 命令执行（受 dangerous_commands.json 规则拦截） |
| `git_ops` | Git 操作（提交前检查敏感文件，worktree/path 参数进入 FileGuard） |
| `web_search` / `web_fetch` | 搜索与网页抓取（只允许 http/https；拒绝 localhost、私网、link-local 等目标） |
| `memory_ops` | 记忆操作（search/add，及 episode 的 update/remove） |
| `skill_load` | 按名加载 SKILL.md 全文（技能的运行时"翻书"） |
| `skill_manage` | 技能管理：list/get/create/edit/patch/versions/rollback（内置技能只读） |
| `todo` | 会话内任务列表（用于多步工作进度跟踪） |

### 8.3 执行保护

- 未知工具 / 抛异常 → 一律转成 `ToolResult(is_error=True)`，**工具失败永远不炸主循环**，作为观察值交还 LLM
- 返回 `Error:`、`Memory rejected:` 或非零 `[exit_code=...]` 的工具结果，也视为 `is_error=True`
- 输出超出预算时截断并保存完整输出路径（`tool/truncation.py`）——保护上下文窗口
- `schema.py` 提供 `function_to_schema()`：从 Python 类型注解 + docstring 自动生成 OpenAI 工具 schema（Optional → 非必填，list[X] → array）

### 8.4 MCP 外接（tool/mcp_client.py）

- 自实现的 MCP 客户端（JSON-RPC 2.0，默认协议版 2025-11-25）：initialize 握手 → initialized notification → tools/list 分页发现 → tools/call 调用
- 支持两种 transport：
  - `stdio`：本地启动 MCP server 子进程，通过 stdin/stdout 通信；`env` 会继承当前环境后叠加配置
  - `streamable_http` / `http`：连接远程 MCP endpoint，支持 JSON 或 `text/event-stream` 响应、`MCP-Session-Id`、`MCP-Protocol-Version`
- 发现的工具注册进同一个 ToolRegistry；默认名为 `mcp_<tool>`，配置 `name` / `alias` 后注册为 `mcp_<name>_<tool>`，以避免多个 MCP server 的工具名冲突
- MCP 工具名会规范化为 provider 友好的 ASCII 标识符，并对超长名称做稳定 hash 截断；MCP `isError=true` 会映射为 `ToolResult(is_error=True)`
- 配置示例：

```yaml
mcp_servers:
  - type: stdio
    name: github
    command: ["npx", "-y", "@example/github-mcp"]
    env:
      GITHUB_TOKEN: "..."

  - type: streamable_http
    name: docs
    url: "https://example.com/mcp"
    headers:
      Authorization: "Bearer ..."
```

- 连接失败按 server 隔离并跳过（best-effort）；失败日志只记录配置摘要和 `headers` / `env` 的 key，不输出 secret 值

---

## 九、技能系统设计

### 9.1 技能 vs 工具 vs 插件

| | 本质 | 形态 | 谁执行 |
|---|---|---|---|
| 工具 | 原子能力（"能做什么"） | Python 函数 | 代码执行，结果回给 LLM |
| 技能 | 方法论（"该怎么做"） | SKILL.md 文档 | LLM 读着它执行（注入 prompt 的 ReAct） |
| 插件 | 事件源 + 响应（"何时被唤起"） | manifest + handler.py | 触发器调 handler，handler 可再调引擎 |

### 9.2 SKILL.md 规范

YAML frontmatter（name/description/version/argument_hint，兼容 trigger/input/output 等扩展字段）+ Markdown 正文（Goal / Process / 分步指引）。与 Claude Code 的 SKILL.md 生态同构，技能可直接互相搬运。

### 9.3 双源加载与版本化

- **内置技能**（`agents/skills/`，当前 28 个）：只读、由 Git 管理；作为技能链节点素材
- **Smith 自装技能**（`~/.agent-smith/agent/skills/`）：同名可覆盖内置（registry 后加载者胜）；可通过 `skill_manage` 工具由 Smith **自己创建和修改**
- **版本控制**（`skill/store.py`）：每次修改前把旧版存入 `.versions/`（保留最近 10 个快照），支持 rollback——Agent 自改技能是高风险操作，可回滚是启用它的前提

### 9.4 执行模型（skill/executor.py）

```
execute_skill(): SKILL.md 全文 + 链上下文 ──作为独立 system prompt──▶ 新开一个 ReAct 循环
```

关键设计：技能执行**不复用**Agent 的 9 段大 prompt，而是用"纯技能视角"的小上下文——让每个链节点专注单一方法论，前序节点产出通过 `context` dict（`{skill}_output`）显式传递，而不是靠共享一条越来越长的对话。rubric 重试的反馈也经由 `context["rubric_feedback"]` 注入。

---

## 十、插件系统设计

### 10.1 Manifest 规范（plugin.json）

```json
{
  "schema": "agentsmith.plugin.v1",
  "name": "github", "version": "0.1.0",
  "description": "...",
  "trigger_type": "polling | webhook | manual",
  "polling_interval_seconds": 60,
  "skills": [ ... ]
}
```

### 10.2 三种触发器（plugin/trigger.py）

| 触发器 | 模型 | 说明 |
|---|---|---|
| PollingTrigger | 常驻 asyncio task，间隔 `poll()` | 轮询外部源（如 GitHub issues）；单事件 handler 异常不打断循环 |
| WebhookTrigger | 无后台任务，HTTP 路由推事件进来 | `handle_event(payload)` 返回 accepted/error |
| CronTrigger | 常驻 task，解析 `M H * * *` 简化 cron | 定时任务（如 daily-report） |

### 10.3 Handler 约定

`handler.py` 暴露 `async def handle(event: dict)`，与工具 provider 同样的**约定式加载**（`plugin/loader.py`）。发现（PluginRegistry.discover 扫 plugin.json）、加载、触发全部 best-effort，坏插件只影响自己。

---

## 十一、安全设计

分层防御，每层独立生效：

| 层 | 机制 | 实现 |
|---|---|---|
| L1 规则拦截 | 工具调用前正则匹配危险模式 | `ToolGuard` + `agents/safety/dangerous_commands.json` |
| L2 文件边界 | 路径白名单 + 敏感目录黑名单；覆盖所有路径型工具 | `FileGuard`（tool_guard.py:17） |
| L3 质量门禁 | 产出必须带真实证据，防"谎报成功" | Gate 体系（6.5） |
| L4 提交防线 | 敏感文件（.env/credentials/密钥）禁止入库 | `PRGate`（gate.py:327） |
| L5 记忆消毒 | 已落库的秘密被周期清除 | Dream 清秘（7.4） |

**L1 规则格式**（安全规则也是内容，不是代码）：

```json
{
  "id": "cmd-inj-001", "tools": ["shell"], "severity": "critical",
  "patterns": ["\\|\\s*(bash|sh|zsh)"],
  "excludePatterns": ["\\|\\s*grep", "\\|\\s*head"],
  "description": "...", "remediation": "..."
}
```

覆盖类别：命令注入、fork 炸弹/死循环/巨型文件（资源滥用）、inline eval/exec（代码执行）、rm -rf / DROP TABLE / git --force / dd 等破坏性命令。`tools` 字段限定规则作用域；`excludePatterns` 用白名单精修误报（如允许 `| grep`）。

**L2 语义**：默认允许 `$HOME`、`/tmp`、cwd；但 `.ssh/.gnupg/.aws/.kube` **在白名单内也一律拒绝**。shell 命令中的绝对路径会被正则提取后逐一过检；`read_file/write_file/edit_file/grep/glob_files/list_dir/git_ops/shell.cwd` 等路径参数也进入同一套 FileGuard。

**网络语义**：`web_fetch` 只允许 http/https，并拒绝 localhost、loopback、private、link-local、reserved、unspecified 等地址。Agent 是本地进程，不应把用户机器的内网访问能力变成默认外部抓取能力。

**统一处置哲学**：拦截 ≠ 终止。所有拦截以 `[BLOCKED] 原因` 作为工具观察返回，LLM 得知边界后自行调整方案；连续 3 次受阻才触发"换方法"提示（6.4）。

---

## 十二、配置体系

五级合并（`engine/llm/model_config.py:34`），**下层覆盖上层已有字段，未填字段继承**：

```
env（AGENTSMITH_LLM_*，最低）
  < 平台 ~/.agent-smith/config.yaml
    < Smith 出厂 agents/smith/config.yaml
      < Smith 运行时 ~/.agent-smith/agent/config.yaml
        < 会话 session_override dict（最高）
```

合并语义（`common/yaml_utils.py` 的 `merge_configs`）：深合并；**值为 None 视为"未设置"跳过**——所以模板里 `model: null` 表达"继承平台配置"而不是"清空"。产出统一的 LLM 配置（api_key/base_url/model/provider/stream），engine 只认 OpenAI-compatible 端点（`llm/client.py`：重试 3 次、超时 300s、支持 prefix_cache_key）。

---

## 十三、服务端集成契约

engine 通过 `engine.execution` 暴露稳定运行契约，server 不导入
`execution.orchestration`、`pipeline` 或 `react` 实现：

```python
from engine.execution import (
    EngineRequest,
    RuntimeContext,
    RuntimeServices,
    reply_with_runtime,
    run_stream_with_runtime,
)
```

`SessionService`（`server/app/services/session_service.py`）的职责边界：会话存在性校验 → 用户消息落库 → 调 engine → 助手消息落库 → SSE 组帧。Pipeline、Gate、ReAct 与 Smith UI 校验均封装在公共执行边界之后；observability Adapter 由 `server/app/services/engine_runtime.py` 在组合时注入。

---

## 十四、设计权衡与已知局限

诚实清单（均为当前代码事实，非猜测）：

### 14.1 有意的取舍

| 取舍 | 理由 |
|---|---|
| 正则门禁而非全 LLM 评审 | 快、免费、可解释；语义漏洞由 LLMGate 兜（仅 2 个关键节点付 LLM 成本） |
| 关键词路由而非全 LLM 路由 | 路由是高频操作；`route_task_with_llm` 已实现 LLM 兜底但主入口暂未启用 |
| 文件存储而非数据库 | 人格/记忆/技能都是用户可审阅资产；SQLite 只做可重建索引 |
| 每次请求重建 Registry/连 MCP | 实现简单、无状态残留；代价是 MCP 冷启动延迟（演进方向：常驻连接池） |
| 单一 OpenAI-compatible LLM 客户端 | 国产/自建推理端点全兼容此协议；多 provider 抽象是伪需求（YAGNI） |

### 14.2 已知不对称与待办

1. **Checkpoint 只在流式链路写**：`run_agent_stream` 每节点存档，`run_agent` 不存
2. **门禁把 `fail` 与 `retry` 同等处理**：verdict 三值设计，但主循环只二分 pass / 非 pass
3. **CronTrigger 是简化 cron**：只支持 `M H * * *` 的时分字段
4. **向量检索依赖 LLM base_url 提供 `/embeddings`**：常见推理端点（如 GLM）不带 jina embedding，向量路会静默降级为纯 FTS5
5. **durable 召回仍是关键词匹配**：当前不依赖向量服务，同义改写可能漏召回

已修复（2026-07-05）：

- ~~流式路径不落对话记忆~~ → `reply_stream()` 现按事件流跟踪工具使用并调用 `save_conversation_memory`（顺带修正了非真实的 `had_tools=True`）
- ~~长期记忆整份常驻 prompt~~ → `search_relevant_memories()` 现在按当前消息召回匹配 durable 条目和 FTS5 episodes，`assemble_memory(include_durable=False)` 只常驻 recent
- ~~会话历史未进 ReAct 上下文~~ → `SessionService` 取最近 10 条消息经 `history` 参数传入 `reply`/`reply_stream`，拼进 base_messages

### 14.3 演进方向

- Checkpoint 打平到非流式链路
- 路由主入口切换到 `route_task_with_llm`
- Registry/MCP 连接常驻化（进程级缓存）
- 独立 embedding 端点配置（与 LLM base_url 解耦），让向量检索真实生效
- 门禁 verdict 三态语义完整化（fail 直接 Blocked、retry 走 guard）

---

## 附录：文件地图

| 子系统 | 文件 | 行数 | 职责 |
|---|---|---|---|
| 执行接口 | `engine/execution/__init__.py` | — | Server 可使用的运行契约与入口 |
| 编排 | `engine/execution/orchestration/lifecycle.py` | — | Run 生命周期、状态投影、清理与终态 |
| 编排 | `engine/execution/orchestration/preparation.py` | — | 路由、工具、Skill、Memory 与 Prompt 准备 |
| 编排 | `engine/execution/orchestration/agent_loop.py` | — | Direct/Pipeline 分派 |
| Pipeline | `engine/execution/pipeline/skill_chain.py` | — | SkillNode/SkillChain 与 YAML 解析 |
| Pipeline | `engine/execution/pipeline/gate.py` | — | Gate 实现 |
| Pipeline | `engine/execution/pipeline/backtrack.py` | — | FailureLoopGuard 状态机 |
| Pipeline | `engine/execution/pipeline/checkpoint.py` | — | SessionCheckpoint 断点存取 |
| ReAct | `engine/execution/react/react_loop.py` | — | 单步 ReAct 事件循环 |
| 路由 | `engine/execution/routing/task_router.py` | — | Identity/关键词/LLM 路由 |
| 执行契约 | `engine/execution/events.py` | — | ExecutionEvent 流式契约 |
| 可观测性 | `engine/observability/trace_store.py` | — | 脱敏、限长、本地 JSONL trace |
| 可观测性 | `engine/observability/recorder.py` | — | 记录边界与执行控制投影扇出 |
| 可观测性 | `engine/observability/runtime.py` | — | 由组合根注入的 RunObservation Adapter |
| 可观测性 | `engine/observability/reader.py` | — | 服务层唯一查询门面 ObservabilityReader |
| 可观测性 | `engine/observability/incidents.py` | — | 从摘要与脱敏 trace 派生 Run Incident |
| 可观测性 | `engine/observability/diagnosis.py` | — | 结构化 RCA：失败节点、证据和建议 |
| 可观测性 | `engine/observability/health.py` | — | 跨 Run 健康度与成本指标 |
| 可观测性 | `engine/observability/proposals.py` | — | 只生成、必须审批的改进提案 |
| 可观测性 | `engine/observability/projections.py` | — | RunSummary 派生指标 |
| 可观测性 | `engine/observability/summary_store.py` | — | 私有、原子写入的历史 Run 聚合记录 |
| Context | `engine/context/assembler.py` | — | Prompt 分层组装、稳定层 hash |
| Context | `engine/context/compression.py` | — | token 预算、裁剪、会话压缩 |
| 记忆 | `engine/memory/store.py` | — | recent.jsonl 证据写入、durable/episode 召回、编译/Dream 调度 |
| 记忆 | `engine/memory/policy.py` | — | 加载唯一 MemoryPolicy、解析三视图路径与结构校验 |
| 记忆 | `engine/memory/compile.py` | — | context + recent + durable 编译审核、offset、指纹与原子提交 |
| 记忆 | `engine/memory/search.py` | — | 可自愈的 FTS5 trigram 索引 |
| 记忆 | `engine/memory/dream.py` | — | 全层危险行清洗 + durable 审核式整理 |
| 记忆 | `engine/memory/user_learner.py` | — | 偏好检测 + 置信度证据信号（不直接写 Markdown） |
| 记忆 | `engine/memory/maintenance.py` | — | Idle/Daily 生命周期维护 |
| LLM | `engine/llm/client.py` | 124 | OpenAI-compatible 客户端（重试/流式/prefix cache） |
| LLM | `engine/llm/replay.py` | — | Provider 响应录制与确定性回放 |
| 工具 | `engine/tool/registry.py` | 97 | provider 加载、schema、执行与截断 |
| 工具 | `engine/tool/ledger.py` | — | 副作用调用账本与恢复保护 |
| 工具 | `engine/tool/snapshot.py` | — | 会话快照 |
| MCP | `engine/mcp/{client,config,session_pool}.py` | — | MCP 连接、窄参数注册与会话池 |
| 技能 | `engine/skill/{loader,registry,executor,store}.py` | 326 | SKILL.md 解析/双源注册/注入执行/版本化 |
| 安全 | `engine/safety/tool_guard.py` | 296 | ToolGuard + FileGuard |
| 安全 | `engine/safety/fact_gate.py` | 411 | 事实门禁（LLM 产出事实性校验） |
| 安全 | `engine/safety/tool_policy.py` | 73 | 工具策略（能力边界声明） |
| 内容 | `agents/smith/` | — | 6 文件 Smith 身份种子 |
| 内容 | `agents/skills/` × 28 | — | 内置技能（code-review / tdd / grilling / research 等） |
| 内容 | `agents/tools/` × 14 | — | 工具 provider |
| 内容 | `agents/safety/dangerous_commands.json` | — | L1 安全规则（29 条 / 9 类） |
| 配置 | `engine/llm/model_config.py` | 81 | 五级配置合并 + LLM 客户端构建 |

---

## 附录 A：与主流框架对比

### A.1 为什么自研？

核心问题：**LLM 不可靠但有创造力，代码可靠但没创造力。怎么结合？** 下表列出主流框架在这一问题上的短板：

| 框架 | 核心问题 |
|---|---|
| **LangChain** | 过度抽象——5-6 层 wrapper，调试极难。Agent 纯 ReAct，没有代码级质量保障 |
| **CrewAI** | 流程控制全靠 LLM prompt——说"先规划再编码"，它可能跳过规划 |
| **AutoGen** | Agent 间对话式协作，但没有质量门禁——Agent A 说什么 B 直接信 |
| **AgentScope** | 纯 ReAct，LLM 决定一切，没有确定性编排层 |
| **Dify/Coze** | 可视化 DAG，无法做条件回退和失败循环检测 |

### A.2 与主流的根本区别

```
纯 ReAct（LangChain）: LLM 决定一切 → 不可控
纯 DAG（Dify）:        代码决定一切 → LLM 只填槽
Agent-Smith:           DAG 控制宏观 → 每节点内 ReAct → 节点间门禁 → 失败可回退
```

不是"用 LLM 做 Agent"，而是**"用代码搭骨架，让 LLM 在骨架内自由发挥"**。

### A.3 完整维度对比

| 维度 | Agent-Smith | LangChain | CrewAI | AutoGen | Dify |
|---|---|---|---|---|---|
| **架构** | DAG+ReAct 混合 | 纯 ReAct | 多Agent对话 | Agent消息 | 可视化DAG |
| **执行控制** | 代码宏观+LLM微观 | LLM控制 | LLM控制 | LLM控制 | 代码控制 |
| **质量保证** | 13 Gate+LLM验证 | 无 | 无 | Agent互评 | 无 |
| **失败处理** | LoopGuard+回退 | 无 | 无 | 重发消息 | 固定重试 |
| **记忆** | 双作用域+4层编译+混合检索 | BufferMemory | 共享 | 对话历史 | 变量传递 |
| **安全** | 29规则+FileGuard+记忆过滤 | 无 | 无 | 无 | 沙盒 |
| **学习** | 偏好自学+技能自进化 | 无 | 无 | 无 | 无 |
| **可观测** | 事件流+会话恢复 | LangSmith(付费) | 日志 | 日志 | 日志 |

---

## 附录 B：常见问答

以下 20 个问答覆盖框架设计的高频问题，每条均可追溯到前文对应章节的设计细节。

1. **为什么不用 LangChain？** → 过度抽象+纯 ReAct 不可控。需要代码级质量保障。
2. **和 Assistants API 区别？** → 在 function calling 上加了技能链/门禁/回退/记忆/安全。
3. **怎么防死循环？** → max_iters=20 + 3失败换策略 + FailureLoopGuard + max_backtracks=5。
4. **怎么做质量控制？** → 双层门禁（SkillRubricGate + 节点 Gate），关键 Gate 有 LLM 验证。
5. **记忆怎么防泄露？** → 写入拒绝密钥 + Dream 8种正则 + 编译 prompt 约束只记画像。
6. **DAG+ReAct 灵感？** → QoderWake 逆向分析。"硬骨架+软肌肉"。
7. **流式怎么保持一致？** → async generator 全程 yield 事件 + context dict 累积 + 检查点。
8. **怎么加新工具？** → agents/tools/ 新建 .py + TOOL_META + execute。无需改代码。
9. **怎么扩展能力？** → agents/skills/ 新建 `SKILL.md`。Smith 不新增身份。
10. **分层架构好处？** → 测试隔离。换 Flask 只改 server/；换 Web/macOS 前端只改客户端层。
11. **config 为什么四层？** → 灵活性平衡。全局→角色→个性化→临时。只需配最高优先级。
12. **技能和工具为什么分？** → 粒度不同。工具=原子操作，技能=完整工作流+多次 LLM 迭代。
13. **MCP 是什么？** → Anthropic 标准化工具协议。STDIO JSON-RPC 桥接到 ToolRegistry。
14. **正则能被绕过？** → 能。纵深防御：正则→FileGuard→输出截断→记忆过滤。
15. **偏好学习置信度？** → 4维检测+独立计数器+同特征3次才写入+不覆盖手写。
16. **重新设计改什么？** → Gate 全 LLM 验证 + 并行节点 + 文件记忆迁移 SQLite。
17. **Dream 名字来源？** → 人类睡眠记忆巩固。去噪、合并、提炼。
18. **9层 Prompt 太长？** → 实测 2500 tokens。token 预算管理是预防。
19. **最坏多少次 LLM 调用？** → 4×5×20 = 400次。实际不会到。
20. **多少行代码？** → ~3300行 Python，零 Agent 框架依赖。httpx+aiosqlite+pyyaml。
