# 06 · Agents 内容层

> **当前实现说明**：`agents/` 是可版本化的内容与本地 provider 目录。它不保存运行时 profile 副本，也不实现插件系统；运行时状态位于 `~/.agent-smith/`。

> **目录级地图**：逐目录职责、边界澄清（`smith/` vs `identities/`、`conditions/` vs `gates/` 等）与内容契约速查见 [`agents/README.md`](../agents/README.md)。

## 目录与职责

| 路径 | 内容 | 被谁加载 |
| --- | --- | --- |
| `agents/smith/` | Smith 的静态身份提示片段：`role.md`、`style.md`、`workflow.md`、`toolbox.md`、`context.md`；另含机器配置 `config.yaml`（不进 prompt，见下节） | md → 运行时 Prompt 装配；`config.yaml` → `engine/llm/model_config.py` 与 `preparation.py` |
| `agents/smith/hooks.yaml` | 内置 hook 配置：4 个内置 hook（`config_protection`、`console_warn`、`cost_tracker`、`quality_gate`）的启用声明 | `engine/execution/hooks/tool/loader.py`，经 `preparation.py` 装入 `HookRegistry`；用户级 hook 另从 `~/.agent-smith/hooks.yaml` 加载 |
| `agents/smith/hooks/` | 上述内置 hook 的 Python 实现 | 同上（按 `hooks.yaml` 中的 `module` 路径动态加载） |
| `agents/identities/` | 声明式身份档案（YAML） | `engine.identity.IdentityCatalog` |
| `agents/pipelines/` | identity route 对应的 skill chain | `SkillChain` |
| `agents/skills/` | shipped `SKILL.md` 技能及其参考资料 | Skill loader；安装时物化到 builtin 路径 |
| `agents/tools/` | 内建 Python tool provider | Tool registry |
| `agents/subagents/` | 声明式子 Agent 类型（YAML）：提示词、工具白名单、模型角色、迭代与 token 上限 | `engine.execution.subagent.SubAgentCatalog`，经 `bind_sub_agent_tool()` 注入 |
| `agents/gates/` | pipeline/skill 可复用的 gate 实现 | Pipeline gate loader |
| `agents/conditions/` | pipeline 的条件函数 | Pipeline loader |
| `agents/safety/` | 声明式危险命令规则 | ToolGuard |
| `agents/output_style.md` | 输出风格素材 | Prompt 装配 |

## `agents/smith/`：一次性种子，而非运行时事实来源

`agents/smith/` 是**首次安装的一次性种子**：`server/app/infrastructure/profile_files.py`
的 `init_smith_profile_files` 只在目标文件不存在时复制。对已有安装修改此目录不生效；
运行时的事实来源是 `~/.agent-smith/agent/`。

### `config.yaml` 是机器配置，不进 prompt

Prompt 装配（assembler）只读取 6 个 md（`role.md`、`style.md`、`workflow.md`、
`toolbox.md`、`context.md` 与 `agents/output_style.md`）。`config.yaml` 不进 prompt，
它是机器配置，承担两件事：

- **`llm`**：五层 merge 中的一层（env 覆盖 → `~/.agent-smith/config.yaml` →
  本文件 → `~/.agent-smith/agent/config.yaml` → 会话级 override），由
  `engine/llm/model_config.py` 解析；
- **`tools.enabled`**：严格白名单（fail-closed），由 `preparation.py` 读取运行时
  副本并与 identity 的 allowlist 取交集。

### Hook 内容契约

`agents/smith/hooks.yaml` 声明工具执行生命周期的挂点实现，分 `pre` / `post` /
`stop` 三类：pre hook 在工具调用前执行、可阻断；post hook 在调用后观察、只产出警告；
stop hook 在每次 Agent 响应结束时执行。每个条目的字段：

- `id`：hook 标识；
- `enabled`：是否加载（`false` 时跳过）；
- `module`：实现文件路径（相对配置文件所在目录解析）；
- `class`：实现类名，须实现 `PreToolHook` / `PostToolHook` / `StopHook` 之一；
- `priority`（pre hook）：数字越小优先级越高；
- `description`：一句话说明。

加载方是 `engine/execution/hooks/tool/loader.py`，在 `preparation.py` 中先装入内置
`agents/smith/hooks.yaml`，再追加加载用户级 `~/.agent-smith/hooks.yaml`（如存在），
统一注册进 `HookRegistry`。

## 身份：声明能力档案，而非多 Agent

每个 `agents/identities/*.yaml` 都采用 `agentsmith.identity/v1`。最小结构如下：

```yaml
schema: agentsmith.identity/v1
id: research
name: Research Agent
description: 面向证据收集的身份。
default: false

prompt:
  role: 先建立证据边界，再给出可追溯结论。

tools:
  enabled: [read_file, web_search, web_fetch]
skills:
  enabled: [research]

routes:
  - id: investigate
    keywords: [research, investigate, 调研]
    pipeline: null
    priority: 10
```

约束：

- catalog 中必须且只能有一个 `default: true`；identity id 和 route id 均不能重复；
- `tools.enabled` 与 `skills.enabled` 是 allowlist。显式写出的 skill 必须可被 loader 发现；
- route 的 `pipeline` 必须引用存在的 pipeline；`null` 表示用该 identity 的权限和提示直接运行 ReAct；
- identity 只影响一次 Run 的能力配置和 Prompt，不能创建独立的 server profile、会话域或并行 worker。

现有 `smith.yaml` 为默认身份（声明一条 `git` 路由，无 pipeline）；`coding.yaml` 是编码能力域身份，声明三条意图路由，各自绑定一条 pipeline（见下节）。

## Pipeline：可审查的技能编排

当前有三条 shipped pipeline，位于 `agents/pipelines/`，由 `coding` 身份的三条路由绑定：

| pipeline | 步骤（skill → gate） | 备注 |
|---|---|---|
| `requirements-research` | `grilling`→`grilling_complete`、`research`→`research_brief`、`ecc-plan`→`plan_confirmed` | 第 1 步（`grilling`）与第 3 步（`ecc-plan`）含 `await_user_input_marker`（`requirements-research.yaml:8,34`） |
| `tdd-development` | `diagnosing-bugs`→`red_loop`、`tdd-workflow`→`tdd_evidence`、`verification-loop`→`tdd_verification` | `diagnosing-bugs` 节点带条件 `coding_bugfix_needs_diagnosis` |
| `code-review` | `code-review`→`review_report`、`verification-loop`→`review_verification` | 首节点含 `await_user_input_marker` |

`tdd-development.yaml` 是带条件节点的示例：

```yaml
route: tdd-development

steps:
  - skill: diagnosing-bugs
    gate: red_loop
    condition: coding_bugfix_needs_diagnosis
    allowed_tools: [read_file, write_file, edit_file, list_dir, glob_files, grep, shell]
    instructions: |
      # 节点的执行指令；只有该节点内的 skill 可以看到
      ...
```

每个步骤的关键字段：

- `skill`：`agents/skills/` 中可发现的技能名；
- `gate`：`agents/gates/` 中已注册的 gate 名，输出先过 gate，通过才提交并进入下一步；
- `condition`（可选）：`agents/conditions/` 中导出的条件函数，返回 false 时跳过该节点；
- `await_user_input_marker`（可选）：skill 输出中出现该标记时，pipeline 保存 checkpoint 并暂停等待用户回复；
- `infer_await_user_input_from_question`（可选，布尔）：无显式 marker 时，若节点输出是一个提问，则推断为需要暂停等待用户输入；
- `allowed_tools`：该节点可用的工具子集，必须落在 identity 的 `tools.enabled` 内；
- `instructions`：注入该节点 skill 的执行指令，只能描述流程，不能绕过安全策略。

`allowed_tools` 是四层收敛链的最后一环，逐层只能收窄：registry 硬编码文件名白名单
`_BUILTIN_PROVIDER_FILENAMES`（20 个）→ profile `config.yaml` 的 `tools.enabled`
（18 个）→ identity 的 `tools.enabled` → 节点 `allowed_tools`。

profile 那一层排除了两个，语义**完全不同**：

- `memory_ops` 被排除是**正确的**——它声明了 `"hidden": True`，本就不面向模型，
  由引擎内部调用；`enabled_tools_from_config()` 对这种名字会记一条 warning 并忽略。
- `sub_agent` 被排除是**遗漏**——它不是 `hidden`，是一个正常的模型可见工具，但上线它的
  `9180061` 没有同步这份种子。后果是能力已实现却对模型不可见（`registry.py:703`
  会拦下调用）。详见 [24 · 子 Agent 委派系统](24-子Agent-委派系统.md)开头的状态说明。

这条链的教训：能力可达需要**每一层都点头**，而"不在 `tools.enabled` 里"既可能是
刻意隐藏，也可能是忘了加——两者从配置文件上看不出区别，只能回到 `TOOL_META` 判断。

pipeline 顶层除 `route`、`steps`、`backtrack` 外还支持可选的 `base_gate`/`base_gates`：
兜底 gate 层，每个节点产出先过它、再过节点自己的 gate。当前三条 shipped pipeline 均未使用。

运行时严格按 YAML 顺序完成每个未被 `condition` 跳过的节点：先执行该节点的 Skill；若
Skill 缺失、被禁用、运行失败，或产出未通过 gate，则在**当前节点**以 ReAct 继续尝试。回退
携带已通过节点的产物、当前 `instructions`、`allowed_tools` 和同一套 gate；因此它不是跳过
节点，也不是把整条链改为直接 ReAct。

注意区分两个阶段：

- **启动期资产校验**（`engine/execution/assets.py` 的 `validate_execution_assets`）：
  校验 route → pipeline → 节点 skill → identity `skills.enabled` → 节点 `allowed_tools`
  的完整闭包；任一引用缺失即抛 `IdentityCatalogError` 并拒绝启动；
- **运行期节点内 ReAct 回退**（`pipeline.py`）：skill 缺失、被禁用、执行失败或
  gate 失败时，在当前节点以 ReAct 继续（即上一段描述的行为），不影响启动。

gate 方面：启动时共注册 20 个 gate（`agents/gates/common/` 5 个 + `agents/gates/coding/`
15 个），三条 shipped pipeline 只使用其中 8 个，其余 12 个为备用/未引用；
`agents/conditions/` 只注册 1 个条件函数 `coding_bugfix_needs_diagnosis`。

不要把 side effect 或真实测试结果只写在 Markdown 说明中：它们必须由 skill 的工具调用和 gate 的事实检查产生。`SkillChain` 引擎支持 gate 失败后按 `backtrack` 映射回退，但当前 shipped pipeline 均未声明 `backtrack`。

## Skill：任务 SOP

一个可加载技能以 `SKILL.md` 为入口，frontmatter 至少提供 `name` 和 `description`，正文写明目标、输入、步骤、限制和可验证输出。技能可以附带 `references/`、`scripts/` 等资源；所有相对路径都以该 skill 目录解析。

`agents/skills/` 现有 24 个目录，其中 8 个无顶层 `SKILL.md`（`codebase-design`、
`domain-modeling`、`git-guardrails-claude-code`、`improve-codebase-architecture`、
`prototype`、`setup-matt-pocock-skills`、`tdd`、`triage`）——它们是上游素材/参考目录，
loader 不会加载它们，也不报错；可加载技能为 16 个。

原则：

1. Skill 描述“如何完成一类任务”，不复制系统安全策略；
2. 所有外部副作用仍由 ToolPolicy、ToolGuard 和审批链决定；
3. `SkillRegistry` 的 enabled 状态由用户控制；若关闭了 Pipeline 已声明的 skill，
   引擎会在对应节点以 ReAct 补偿，仍必须通过该节点 gate，不能静默跳过；
4. 内部新建技能必须附带至少一个可验证示例或相应回归测试；vendored 上游技能
   （见 `agents/skills/SOURCES.md`）豁免——16 个带 `SKILL.md` 的技能中，目前仅
   5 个 `coding-*` 技能附带 `references/evals.md`。

## Tool provider：窄接口、统一治理

`agents/tools/*.py` **不是**按 glob 自动发现的：registry 只加载
`engine/tool/registry.py` 中硬编码文件名白名单 `_BUILTIN_PROVIDER_FILENAMES`
（20 个）内的文件。一个 provider 声明工具元数据并提供异步 `execute(**kwargs)`；schema 会在 registry 构建时校验。

20 个内建工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob_files`、
`grep`、`shell`、`git_ops`、`read_pdf`、`render_pdf_page`、`web_search`、`web_fetch`、
`web_crawl`、`todo`、`skill_manage`、`skill_load`、`memory_ops`、`render_ui`、
`get_current_time`、`sub_agent`。
其中 `memory_ops` 与 `skill_load` 已注册但不在默认 profile 的 `tools.enabled`
白名单内，默认调不到。

新增工具的最低要求：

0. 把文件名加入 `engine/tool/registry.py` 的 `_BUILTIN_PROVIDER_FILENAMES`——否则文件根本不会被加载；
1. 定义精确、可 JSON 编码的输入 schema 和有界输出；
2. 在 provider 内不重复实现权限模型，也不直接逃逸到未经 guard 的路径；
3. 有文件/命令/网络副作用时补齐 `ToolGuard`、`ToolPolicy` 和审批测试；
4. 在身份 allowlist、skill 引用和文档中只暴露已经注册且验证过的名称。

## MCP

Smith 是 MCP client。配置的 stdio 或 streamable HTTP server 由 `engine.mcp` 建立连接、发现工具，并以受规范化的名字注册进同一个 `ToolRegistry`。MCP 工具与本地 provider 共用 policy、guard、审批、ledger 和事件协议。

当前没有 MCP server、plugin manifest、webhook trigger 或 marketplace。不要在身份/pipeline 内容中假定这些接口已经存在。

## 运行时可编辑内容

`~/.agent-smith/agent/skills/` 用于用户安装技能；`builtin/skills/` 用于发行版携带的技能，两者不能混为一类。`context.md`、`memory/recent.md` 与 `memory/durable.md` 是受 Memory Policy 维护的视图；`SMITH.md` 则由用户维护，自动学习不能覆盖它。详见[记忆系统](05-Engine-记忆系统.md)。
