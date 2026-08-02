# 06 · Agents 内容层

> **当前实现说明**：`agents/` 是可版本化的内容与本地 provider 目录。它不保存运行时 profile 副本，也不实现插件系统；运行时状态位于 `~/.agent-smith/`。

> **目录级地图**：逐目录职责、边界澄清（`smith/` vs `identities/`、`conditions/` vs `gates/` 等）与内容契约速查见 [`agents/README.md`](../agents/README.md)。

## 目录与职责

| 路径 | 内容 | 被谁加载 |
| --- | --- | --- |
| `agents/smith/` | Smith 的静态身份提示片段：`config.yaml`、`role.md`、`style.md`、`workflow.md`、`toolbox.md`、`context.md` | 运行时 Prompt 装配 |
| `agents/identities/` | 声明式身份档案（YAML） | `engine.identity.IdentityCatalog` |
| `agents/pipelines/` | identity route 对应的 skill chain | `SkillChain` |
| `agents/skills/` | shipped `SKILL.md` 技能及其参考资料 | Skill loader；安装时物化到 builtin 路径 |
| `agents/tools/` | 内建 Python tool provider | Tool registry |
| `agents/gates/` | pipeline/skill 可复用的 gate 实现 | Pipeline gate loader |
| `agents/conditions/` | pipeline 的条件函数 | Pipeline loader |
| `agents/safety/` | 声明式危险命令规则 | ToolGuard |
| `agents/output_style.md` | 输出风格素材 | Prompt 装配 |

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
| `requirements-research` | `grilling`→`grilling_complete`、`research`→`research_brief`、`ecc-plan`→`plan_confirmed` | 前两步含 `await_user_input_marker` |
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
- `allowed_tools`：该节点可用的工具子集，必须落在 identity 的 `tools.enabled` 内；
- `instructions`：注入该节点 skill 的执行指令，只能描述流程，不能绕过安全策略。

不要把 side effect 或真实测试结果只写在 Markdown 说明中：它们必须由 skill 的工具调用和 gate 的事实检查产生。`SkillChain` 引擎支持 gate 失败后按 `backtrack` 映射回退，但当前 shipped pipeline 均未声明 `backtrack`。

## Skill：任务 SOP

一个可加载技能以 `SKILL.md` 为入口，frontmatter 至少提供 `name` 和 `description`，正文写明目标、输入、步骤、限制和可验证输出。技能可以附带 `references/`、`scripts/` 等资源；所有相对路径都以该 skill 目录解析。

原则：

1. Skill 描述“如何完成一类任务”，不复制系统安全策略；
2. 所有外部副作用仍由 ToolPolicy、ToolGuard 和审批链决定；
3. `SkillRegistry` 的 enabled 状态由用户控制，关闭的 skill 不会被 pipeline 执行；
4. 新 skill 必须附带至少一个可验证示例或相应回归测试。

## Tool provider：窄接口、统一治理

`agents/tools/*.py` 是被发现的 provider。一个 provider 声明工具元数据并提供异步 `execute(**kwargs)`；schema 会在 registry 构建时校验。常见内建工具包括文件读写、目录/文本检索、shell、Git、PDF、网络、Todo、技能管理与 UI 渲染。

新增工具的最低要求：

1. 定义精确、可 JSON 编码的输入 schema 和有界输出；
2. 在 provider 内不重复实现权限模型，也不直接逃逸到未经 guard 的路径；
3. 有文件/命令/网络副作用时补齐 `ToolGuard`、`ToolPolicy` 和审批测试；
4. 在身份 allowlist、skill 引用和文档中只暴露已经注册且验证过的名称。

## MCP

Smith 是 MCP client。配置的 stdio 或 streamable HTTP server 由 `engine.mcp` 建立连接、发现工具，并以受规范化的名字注册进同一个 `ToolRegistry`。MCP 工具与本地 provider 共用 policy、guard、审批、ledger 和事件协议。

当前没有 MCP server、plugin manifest、webhook trigger 或 marketplace。不要在身份/pipeline 内容中假定这些接口已经存在。

## 运行时可编辑内容

`~/.agent-smith/agent/skills/` 用于用户安装技能；`builtin/skills/` 用于发行版携带的技能，两者不能混为一类。`context.md`、`memory/recent.md` 与 `memory/durable.md` 是受 Memory Policy 维护的视图；`SMITH.md` 则由用户维护，自动学习不能覆盖它。详见[记忆系统](05-Engine-记忆系统.md)。
