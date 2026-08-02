# agents/ — 内容层地图

`agents/` 是 Smith 的**内容层（content layer）**：不是 Python 包（无 `__init__.py`），
也不含执行逻辑。这里只放"素材"——身份声明、pipeline 编排、skill 方法体、工具 provider、
门禁与条件、危险命令规则。全部执行框架在 `engine/`，由引擎在启动时按约定扫描并加载本目录。

**铁律**：本目录任何文件不得 `import` engine / server / common。内容通过注入的辅助函数
（`output_key`、`environment`、`runtime` 等）访问引擎能力；契约是"命名约定 + 鸭子类型"，
不是类型继承。架构边界由 `engine/tests/architecture/` 强制验证。

## 目录职责与加载方

| 路径 | 内容 | 被谁加载 |
| --- | --- | --- |
| `smith/` | 默认身份种子：`config.yaml` + `role.md` / `style.md` / `workflow.md` / `toolbox.md` / `context.md` | `engine/context/assembler.py` 逐层装配 |
| `identities/` | 声明式领域身份（YAML `agentsmith.identity/v1`） | `engine/identity/catalog.py` |
| `pipelines/` | SkillChain 编排定义（YAML） | `engine/execution/pipeline/skill_chain.py` |
| `skills/` | 任务 SOP（SKILL.md 方法体） | `engine/skill/registry.py` |
| `tools/` | 内建工具 provider（`TOOL_META` + `execute`） | `engine/tool/registry.py` |
| `gates/` | pipeline 门禁实现（校验节点产出） | `engine/execution/pipeline/skill_chain.py` |
| `conditions/` | pipeline 步骤条件函数（决定节点是否跳过） | 同上 |
| `safety/` | 声明式危险命令规则（仅数据） | `engine/safety/tool_guard.py` |
| `output_style.md` | 全局输出纪律（所有身份生效） | `engine/context/assembler.py` |

## 边界澄清

### `smith/` 与 `identities/`

- `smith/` 是**默认身份的 prompt 素材**——怎么想、怎么说、怎么做（role → style → workflow → toolbox → context），只被 prompt 装配器消费。
- `identities/*.yaml` 是**身份的声明**——能力 allowlist + 意图路由，只被身份目录消费。
- 关系：`smith/` 描述"默认人格"，`identities/` 描述"某次任务的领域指令与路由"。
  新增领域身份只需加 `identities/<domain>.yaml` 及它引用的 pipeline/gate/skill，**不动 `smith/`**。

### `output_style.md` 与 `smith/style.md`

- `output_style.md`：对**所有身份、所有输出**生效的硬性纪律（无 emoji、无 mermaid、lead with substance、`render_ui` 时机等）。
- `smith/style.md`：**默认身份的口吻与决策取舍**（怎么说、信息不全时怎么选），预算紧张时会被裁剪。
- 全局纪律不要写进 `smith/style.md`，否则其他身份拿不到。

### `conditions/` 与 `gates/`

- `condition`：布尔判断（`fn(ctx) -> bool`），返回 false 时**跳过整个节点**——决定"做不做"。
- `gate`：对节点产出的判定（`async check(output, ctx) -> GateResult`，verdict `pass`/`retry`/`fail`）——决定"过了没有"。
- 两者在 pipeline YAML 中位于不同键位：`condition` 在节点执行前判断，`gate` 在节点产出后校验。

### `safety/` 与 `engine/safety/`

- `safety/dangerous_commands.json` 只是**声明式规则数据**（正则规则 + excludePatterns）。
- 所有安全机制（ToolGuard、ToolPolicy、FileGuard、审批链、沙箱）实现在 `engine/safety/`。
- 内容层的边界：这里只放"规则"，不放"机制"。

## 内容契约速查

| 载体 | 契约 |
| --- | --- |
| 工具 | 模块级 `TOOL_META`（JSON Schema + 权限元数据）+ `async execute(**kwargs)` |
| 门禁 | 模块级 `GATES = {"key": Factory}`；Gate 实现 `async check(output, ctx) -> GateResult(verdict, reason, retry_hint)`，可选声明 `llm_prompt` 触发 LLM 复核 |
| 条件 | 模块级 `CONDITIONS = {"key": fn}`，`fn(ctx) -> bool` |
| Skill | 目录名 == frontmatter `name`，正文为 Markdown 方法体 |
| 身份 | YAML `schema: agentsmith.identity/v1` |
| Pipeline | YAML `steps:` 节点列表 + 顶层 `base_gate(s)` / `backtrack` |

Pipeline 执行规则：`steps[].skill` 是严格的运行时依赖。引擎必须先从
`SkillRegistry` 解析出同名 `SKILL.md`，再经 `execute_skill_events()` 进入该
Skill 的执行上下文；节点缺少 Skill 时以 `blocked` 结束，**不得**退回为通用
ReAct。被用户禁用的节点 Skill 同样会阻断；只有该节点的 `condition` 返回
`false` 才可跳过。通用 ReAct 只适用于没有绑定 Pipeline 的 route（`pipeline: null`）。

加载规则：

- `gates/`、`conditions/` 递归扫描 `*.py`，**`_` 开头的文件会被跳过**；重复注册的 key、
  语法错误、未知 gate/condition 引用都会在启动时抛错——宁可启动失败，不许静默丢门禁；
- `skills/` 只加载含 `SKILL.md` 的目录；无 `SKILL.md` 的目录**不会加载也不报错**
  （本仓库 `skills/` 下存在若干此类素材目录，不进运行时）；
- `tools/` 只加载白名单内文件名的 provider；节点 `allowed_tools` 必须是 identity
  `tools.enabled` 的子集，且由已注册 provider 提供，否则拒绝启动。

## 新增一个能力域（不改引擎）

1. `identities/<domain>.yaml` — 声明身份与 routes（每条 route 可绑定 pipeline）；
2. `pipelines/<chain>.yaml` — 编排步骤（skill → gate → condition → allowed_tools → instructions）；
3. `skills/` — 补 SKILL.md（若引用的技能尚未注册）；
4. `gates/<domain>/gates.py` 或 `conditions/<domain>.py` — 按需补门禁/条件；
5. 工具引用必须是 `tools/` 或 MCP 已注册的名字，且落在身份 `tools.enabled` allowlist 内。

## 文档索引

- `docs/06-Agents-内容层.md` — 内容层设计权威文档
- `gates/README.md` — 门禁/条件编写规范
- `identities/README.md` — 身份目录契约、路由选择规则与最小格式
- `skills/README.md` — SKILL.md 运行时契约
- `skills/SOURCES.md` — 上游技能来源与锁定版本
- `tools/PRD-2026-07-30-tool-hardening.md` — 工具加固 PRD（含 18 个 provider 的加固记录）
