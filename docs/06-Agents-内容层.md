# 06 · Agents 内容层

本文档描述 Agent-Smith 的 Smith 基础身份、YAML 领域身份目录、内置技能规范、工具 Provider 接口、安全规则和技能自进化机制。

---

## 1. Smith 身份文件格式

Agent-Smith 只有一个运行中的 Agent：Smith。出厂基础人格位于 `agents/smith/`，定义全局工作规约、用户上下文和运行配置；领域身份位于 `agents/identities/*.yaml`，在一次任务中叠加领域指令、能力边界和 intent→pipeline 路由。

具体能力不通过新增 Agent 模板或运行实例扩展，而是通过 `agents/identities/*.yaml`、`agents/pipelines/*.yaml` 与 `agents/skills/*/SKILL.md` 组合声明。

### 1.1 config.yaml — 元信息

```yaml
name: 个人助手
role: personal-assistant
description: 面向个人工作流的常驻本地 Agent，负责理解目标、整理上下文、检索信息、规划执行并交付可落地结果
llm:
  model: null
knowledge:
  - personal-workflow
  - local-context
  - research-and-synthesis
  - execution-planning
  - writing-and-communication
tools:
  enabled:
    - read_file
    - write_file
    - edit_file
    - list_dir
    - glob_files
    - grep
    - shell
    - web_search
    - web_fetch
    - git_ops
    - todo
```

关键字段说明：

| 字段 | 类型 | 含义 |
|------|------|------|
| `name` | string | 显示名称（中文） |
| `role` | string | 固定为 `personal-assistant` |
| `description` | string | 一句话职责描述 |
| `llm.model` | string \| null | LLM 模型名，null 继承上层 |
| `knowledge` | list[string] | 知识领域标签，用于知识库检索 |
| `tools.enabled` | list[string] | 允许使用的工具名单 |

### 1.2 role.md — "我是谁"

定义 Smith 的核心身份：

- **Core Mission** — 一句话使命宣言
- **Non-Negotiable Principles** — 不可协商的原则（通常 4-5 条）
- **Done Criteria** — 什么算"完成"的可验证标准
- **Anti-Goals** — 明确列出"不做什么"，划定职责边界

重要边界：Smith 不根据任务创建或切换到另一个 Agent；它可以在同一个 Smith 运行时中叠加一个 YAML 领域身份，具体 SOP 仍通过 skill 按需加载。

### 1.3 style.md — "我怎么工作"

定义 Smith 的沟通风格和判断习惯：

- **Working Style** — 工作风格描述（自底向上/自顶向下等）
- **Decision Heuristics** — `When <场景>: <决策>` 格式的启发式规则
- **Good Habits** — 推荐的好习惯
- **Anti-Patterns** — 明确禁止的反模式

### 1.4 workflow.md — 全局工作规约

`workflow.md` 不承载具体场景的完整 SOP，而是约束 Smith 什么时候调用 skill、什么时候调用工具、什么时候停下来确认。

典型内容：

- 直接回答、本地上下文、产品/全栈任务、Bug、代码修改、审查、外部事实的路由原则
- 工具调用规约：每次工具调用必须回答一个明确问题
- skill 使用规约：只使用当前已加载的 skill，没有匹配项时走通用流程
- 公共步骤：Align → Gather → Advance → Verify → Deliver
- 停下来确认的条件

### 1.5 toolbox.md — 工具使用原则

`toolbox.md` 只写工具使用原则；实际工具列表由 ToolRegistry 动态注入，避免模板中写了已不存在或未启用的工具。

核心原则：

- 能直接回答的问题不调用工具
- 涉及本地文件/仓库/配置时先读现状再判断
- 写入前确认当前内容和相邻风格
- 命令失败后先分析错误，不连续重复同一失败命令
- Git 操作只围绕当前任务，避免混入无关改动

### 1.6 context.md — 用户偏好

`context.md` 是 Smith 自动学习后形成的常驻用户协作记忆，不再使用占位符，也不由 `UserPreferenceLearner` 直接改写。学习器只产出证据信号，Compiler 与 Reviewer 按 `engine/memory/MEMORY_POLICY.md` 生成完整文件：

```markdown
# Smith Context

## Confirmed Preferences
- **Language**: 默认使用中文回答。

## Collaboration Patterns
- **Answer structure**: 先给结论，再说明必要依据。

## Stable User Context
```

用户手写的长期规则属于 `SMITH.md`；自动记忆不得修改它。context 的完整准入和格式规则以 MemoryPolicy 为准。

### 1.7 已移除的旧结构化人格文件

以下文件不再属于 Smith 身份种子：

- `expertise.json`
- `traits.json`
- `pipeline.json`

旧文件承担的能力画像、风格标签和交付管线职责已经迁移到 skill metadata 和 `SKILL.md` 中。

---

## 2. Smith 基础身份、领域身份与通才 skill

当前内容分为一个基础身份目录和可扩展的领域身份目录：

| 内容目录 | 标识 | 用途 |
|----------|------------|------|
| `agents/smith/` | `personal-assistant` | 唯一常驻 Smith 的基础人格、风格、上下文和运行配置 |
| `agents/identities/*.yaml` | `id` | 领域 prompt、tools/skills allowlist、intent→pipeline 路由；启动时统一扫描 |

仓库附带 **28 个内置 skill**（位于 `agents/skills/`）：

`ask-matt`, `code-review`, `codebase-design`, `diagnosing-bugs`, `domain-modeling`, `edit-article`, `git-guardrails-claude-code`, `grill-me`, `grill-with-docs`, `grilling`, `handoff`, `implement`, `improve-codebase-architecture`, `migrate-to-shoehorn`, `obsidian-vault`, `prototype`, `research`, `resolving-merge-conflicts`, `scaffold-exercises`, `setup-matt-pocock-skills`, `setup-pre-commit`, `tdd`, `teach`, `to-spec`, `to-tickets`, `triage`, `wayfinder`, `writing-great-skills`

此外，用户也可安装额外技能到 Smith 的运行时档案中。

---

## 3. SKILL.md 格式规范

技能是 Markdown 文件，使用 YAML frontmatter 声明元信息：

```yaml
---
name: planning
description: "制定实现计划，输出步骤、验证点和风险项"
version: "1.0"
trigger: task_start
trigger_condition: "(可选) 触发条件描述"
input: "task description or requirement"
output: "numbered implementation plan with verification points"
---

# Planning

## Goal
[目标描述]

## Process
[步骤流程]

## Output Format
[输出格式模板]
```

### Frontmatter 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | Yes | 技能唯一名称，kebab-case |
| `description` | Yes | 一句话描述技能作用 |
| `version` | Yes | 语义化版本号 |
| `trigger` | No | 触发时机（`task_start`, `on_error`, `pre_implement`, `large_change`, `pre_review`, `pre_commit`） |
| `trigger_condition` | No | 触发条件的详细描述 |
| `input` | No | 技能期望的输入描述 |
| `output` | No | 技能产出的输出描述 |

### 正文结构

- **Goal** — 技能的目标
- **Process / Steps** — 执行步骤或流程
- **Output Format** — 输出的 Markdown 模板（通常用代码块给出）

---

## 4. 技能加载

`agents/skills/` 是内置技能入口，当前包含 28 个内置技能。Smith 还会加载用户数据目录下的技能；同名用户技能覆盖内置技能。

Feature / Bug Fix 只执行当前实际加载且匹配预定义链路的技能。如果没有匹配技能，运行时直接回落到普通 ReAct。

---

## 5. 工具 Provider 接口

所有工具位于 `agents/tools/` 目录下，每个 `.py` 文件是一个独立工具 Provider。

### 接口约定

每个工具文件需要导出两样东西：

```python
# 1. TOOL_META — 工具元信息字典
TOOL_META = {
    "name": "tool_name",                    # 唯一标识
    "description": "工具的一句话描述",         # 展示给 LLM
    "parameters": {                          # JSON Schema
        "type": "object",
        "properties": { ... },
        "required": [...]
    }
}

# 2. async def execute(**kwargs) -> str — 异步执行函数
async def execute(*, param1: str, param2: int = 0) -> str:
    # 执行逻辑
    return "结果字符串"
```

### 自动发现机制

`engine/tool/registry.py` 中的 `ToolRegistry.load_providers()` 扫描 `agents/tools/` 目录下所有 `.py` 文件（跳过 `_` 开头的），自动注册发现的工具。无需手动注册。

### 主要活跃工具

| 工具 | 关键参数 | 行为特征 |
|------|---------|---------|
| `read_file` | `path`, `offset`, `limit` | 50KB 上限，返回带行号的文本，超限需用 offset/limit 分段读取 |
| `write_file` | `path`, `content`, `append` | 写入前校验是否在工作目录内，自动创建父目录，支持追加模式 |
| `edit_file` | `path`, `old_text`, `new_text` | 精确文本替换，修改现有文件的局部内容 |
| `shell` | `command`, `timeout`, `cwd` | 默认 30s 超时（最大 120s），输出截断 10KB，通过 `asyncio.create_subprocess_shell` 执行 |
| `git_ops` | `action`, `cwd`, `branch`, `message`, `files`, ... | 8 个操作：status/diff/branch_create/commit/push/worktree_create/worktree_remove/discover。提交前自动检查敏感文件（.env, .pem, .key, .ssh/ 等） |
| `memory_ops` | `action`, `agent_id`, `query`/`content`/`evidence`/`memory_id` | 4 个操作：search/add/update/remove。强制要求 evidence，自动拒绝含敏感信息（API key、密码等）的记忆 |
| `web_fetch` | `url`, `timeout` | 仅允许 http/https，50KB 内容上限，默认 15s 超时（最大 30s），阻止 file/ftp/data scheme |
| `web_search` | `query`, `max_results` | 网络搜索，返回结构化搜索结果 |
| `skill_load` | `name` | 按名称加载 `agents/skills/<name>/SKILL.md`，找不到时列出可用技能 |
| `skill_manage` | `action`, `skill_name`, ... | 7 个操作：list/get/create/edit/patch/versions/rollback。运行时绑定当前 Agent 技能目录；内置技能只读 |
| `todo` | `action`, `items`, ... | 待办事项管理 |
| `grep` | `pattern`, `path`, `options` | 按正则/文本模式搜索文件内容 |
| `glob_files` | `pattern`, `path` | 按 glob 模式查找文件 |
| `list_dir` | `path`, `recursive` | 列出目录内容 |

### 工具输出截断

`ToolRegistry.execute()` 对所有工具输出强制 4000 字符上限，超出部分自动截断并附加 `[truncated]` 标记，以保护 LLM 上下文窗口。

---

## 6. 安全规则

安全规则定义在 `agents/safety/dangerous_commands.json`，由 `engine/safety/tool_guard.py` 在工具执行前检查。

### 9 大类 29 条规则

| 类别 | ID 范围 | 条数 | 典型拦截目标 |
|------|---------|------|-------------|
| `command_injection` | cmd-inj-001 ~ 004 | 4 | 管道到 shell 解释器、反引号/`$()` 命令替换、`eval`、变量注入 |
| `resource_abuse` | res-abuse-001 ~ 003 | 3 | Fork bomb、无退出条件的死循环、创建超大文件 |
| `code_execution` | code-exec-001 ~ 004 | 4 | Python eval/exec 内联、动态 import、`compile()` 滥用、`node -e` 内联执行 |
| `destructive_command` | destruct-001 ~ 004 | 4 | `rm -rf /`、`DROP TABLE`、`git push --force`、格式化磁盘等不可逆操作 |
| `network_abuse` | net-abuse-001 ~ 003 | 3 | 向外部 POST 数据、反向 shell、端口扫描 |
| `sensitive_file_access` | sens-file-001 ~ 004 | 4 | /etc/passwd, .ssh/, .env, .pem/.key 私钥文件 |
| `privilege_escalation` | priv-esc-001 ~ 003 | 3 | sudo/su 提权、chmod 777、setuid/setgid |
| `shell_evasion` | sh-evade-001 ~ 003 | 3 | Base64 解码后管道到 shell、清除 history、覆盖标准命令别名 |
| `platform_integrity` | platform-protect-001 | 1 | 保护 Agent-Smith 平台自身配置和数据不被篡改 |

### 规则结构

每条规则包含：

```json
{
  "id": "cmd-inj-001",
  "tools": ["shell"],             // 适用的工具列表
  "category": "command_injection", // 分类
  "severity": "critical",         // critical 或 major
  "patterns": ["..."],            // 正则匹配模式
  "excludePatterns": ["..."],     // 排除模式（白名单）
  "description": "...",           // 风险描述
  "remediation": "..."            // 修复建议
}
```

**severity 说明**：
- `critical` — 直接阻止执行，不可绕过
- `major` — 警告并要求确认

---

## 7. 技能自进化

### 内置 vs Agent 技能

| 属性 | 内置技能 | Agent 技能 |
|------|---------|---------|
| 位置 | `agents/skills/<name>/SKILL.md` | `~/.agent-smith/agent/skills/<name>/SKILL.md` |
| 可修改 | No（只读） | Yes |
| 版本控制 | Git 跟踪 | SkillStore 版本快照 |
| 来源 | 系统预装 | Agent 创建或进化 |

### SkillStore 版本管理

`engine/skill/store.py` 中的 `SkillStore` 为每个 Agent 技能维护版本快照：

```
~/.agent-smith/agent/skills/<name>/
    SKILL.md            # 当前版本
    .versions/          # 历史快照
        20260704T120000.md
        20260704T130000.md
        ...
```

- 保留最近 **10 个版本**快照，超出自动清理最老的
- 每次 edit 或 patch 操作前自动保存当前版本
- rollback 前也会保存当前版本（不丢失回滚前的状态）
- 支持 `diff` 操作比较任意两个版本（含 `"current"` 特殊值）

### skill_manage 工具 7 个操作

| 操作 | 必填参数 | 说明 |
|------|---------|------|
| `list` | — | 列出所有技能（内置 + 当前 Agent），含来源标记 |
| `get` | `skill_name` | 读取技能内容（优先当前 Agent 版本，其次内置） |
| `create` | `skill_name`, `content` | 创建当前 Agent 技能（不可与内置技能同名） |
| `edit` | `skill_name`, `content` | 全文替换当前 Agent 技能（自动存版本） |
| `patch` | `skill_name`, `section`, `section_content` | 按 Markdown 章节局部替换（自动存版本） |
| `versions` | `skill_name` | 列出版本快照清单 |
| `rollback` | `skill_name`, `version_id` | 回滚到指定版本 |

`list/get/versions` 是只读操作；`create/edit/patch/rollback` 必须通过当前调用的精确审批。写操作成功后，运行时会刷新当前请求使用的技能目录，并重新应用禁用技能和领域身份 allowlist；因此默认身份可立即读取新技能，显式限制 `skills.enabled` 的身份仍只暴露白名单内技能。
