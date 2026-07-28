# Built-in Skills

`agents/skills/` 存放随 Agent-Smith 发布的内置 Skill。

## Skill 是什么

Skill 是面向一类稳定任务的可复用执行方法。它的目标是让 Smith 在不同运行中遵循可预测的过程，而不是把任务包装成新的 Agent。

- **Skill**：说明某类任务该怎样完成。
- **Tool**：提供读文件、执行命令、访问网络等原子能力。
- **SkillChain**：按确定顺序编排多个 Skill，并设置门禁与回退。
- **Smith Profile**：定义全局身份、工作原则和工具边界。

`agents/smith/` 提供所有运行的全局基础规则；被路由选中的身份 YAML 则提供
本次运行的领域指令和工具/Skill allowlist。Skill 保持自包含，只写自身特有的
方法和边界，不重复这两层规则。

## 运行时契约

运行时扫描 `agents/skills/` 的直接子目录，并注册其中的 `SKILL.md`：

```text
agents/skills/
├── README.md
└── <skill-name>/
    ├── SKILL.md
    └── references/
        ├── evals.md         # 内置 Skill 必需；人类可读的最小回归
        └── *.md             # 可选参考，运行时不会自动加载
```

- Skill 名称直接使用简单的 kebab-case，例如 `planning`、`code-review`。
- 目录名必须与 `SKILL.md` frontmatter 中的 `name` 完全一致。
- 注册表和 SkillChain 都通过完整名称精确匹配 Skill。
- 添加 Skill 不会自动把它加入 SkillChain；链路变更必须显式修改并测试 `engine/execution/skill_chain.py`。
- 主 Prompt 只注入 Skill 的名称和描述；Skill 被选中后，`SKILL.md` 正文才作为独立执行上下文加载。
- `README.md` 和 `references/` 不会自动进入执行上下文。

## 最小 `SKILL.md`

运行时解析 `name`、`description`、`version` 和可选的 `argument_hint`。其他 frontmatter 字段目前不产生路由或执行行为。

```markdown
---
name: code-review
description: Review a code change for correctness, security, performance, and maintainability. Use when a user asks for a code or diff review.
version: "1.0.0"
argument_hint: commit, branch, diff, or path
---

# Code Review

## Use When
[该 Skill 应处理的请求]

## Route Elsewhere
[相似但应交给其他 Skill、工具或通用流程的请求]

## Steps
1. [动作]
   Completion: [可检查的完成条件]
2. [动作]
   Completion: [可检查的完成条件]

## Verification
[最终交付必须满足的检查条件]

## Safety
[仅保留该 Skill 特有的副作用边界；无特殊风险时省略]
```

编写要求：

- `description` 同时说明 Skill 做什么、什么请求应触发它；避免同义触发词堆叠。
- `Route Elsewhere` 给出可执行的替代路径，不只写“不要使用本 Skill”。
- 核心步骤必须自包含，每一步都有可检查的完成条件。
- 验证标准描述可观察结果，不使用“尽量完善”“确保质量”等模糊要求。
- Skill 只写会改变执行行为的规则；删除常识、重复说明和已由 Smith Profile 约束的内容。
- 新建顶层 Skill 前，先确认它有独立的触发条件、执行过程和完成标准；否则应作为现有 Skill 的步骤、检查项或 reference。

## References

`references/evals.md` 是随内置 Skill 交付的人类可读回归说明，不参与运行时执行。其他 reference 只承载可选的参数表、长篇指南、错误目录或示例。

引入依赖 reference 的社区 Skill 时，应先把所有执行路径都需要的信息收回 `SKILL.md`；只有实现并测试 reference resolver 后，才能把分支内容放到 `references/` 后面。文件名使用短而具体的名称，例如 `database-migrations.md`、`a11y-checklist.md`。

## 最小评估与资产边界

每个拟内置 Skill 必须在 `references/evals.md` 中提供四类最小用例：

- **trigger**：应命中该 Skill 的输入，以及预期步骤和检查点。
- **route**：相似但应分流的输入，以及明确的替代路径。
- **happy-path**：至少一条可重复的端到端流程；联网、登录或外部配置写明前置条件。
- **guard**：涉及认证、配置或高风险写操作时，预期的检查、确认或阻断行为；无特殊风险时明确标记不适用。

每条用例至少写明输入、预期路由、关键检查点和完成条件。评估关注可路由性、可执行性、可诊断性和可回归性，不评价文案风格。

随 Skill 交付的执行与回归资产为 `SKILL.md`、`references/*.md` 和人类可读回归。机器评分规则、benchmark 配置、runtime adapter 等工程资产统一放在 `tests/skills/<skill-name>/`。

从社区引入 Skill 时，还必须保留可追溯的上游来源并确认许可证允许随产品分发；来源记录和许可证文件不进入运行时上下文。
