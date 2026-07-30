# 身份目录与路由配置

`agents/identities/` 是 Smith 的领域身份目录。服务启动时会扫描其中所有
`.yaml` / `.yml` 文件，并在启动阶段校验：

- 必须且只能有一个 `default: true` 身份；
- 每条 `pipeline` 必须对应 `agents/pipelines/<pipeline>.yaml`；
- 显式声明的 `skills.enabled` 必须存在于内置或 Smith 自装技能目录。

身份不是新的 Agent，也不会创建 `employees/<id>` 一类的运行时档案。Smith
始终是唯一运行中的 Agent；每个身份只是一次任务的领域指令、能力边界与
路由规则。可变记忆和 pipeline 检查点按身份写入：
`~/.agent-smith/agent/identity-state/<identity-id>/`。

## 已发布的 Coding 身份插件

`coding.yaml` 内置三条窄意图 SkillChain。普通编码、修复和重构请求不因
关键词而被劫持，保持普通 ReAct；只有明确意图才进入链。

```text
coding identity
├── requirements-research
│   └── grilling → research → ecc-plan (each user decision pauses safely)
├── tdd-development
│   └── diagnosing-bugs (bug only) → tdd-workflow → verification-loop
└── code-review
    └── code-review → verification-loop
```

`grill-me` 是用户可见入口；它按 Matt 的上游定义委派给 `grilling`，因此实际
链节点是后者。`grilling` 和 ECC `plan` 都可能等待一个用户决策：链会保存当前
节点，下一条同会话、同工作目录的回答从该节点继续；输入 `cancel` 或 `取消` 则
放弃等待并回到普通路由。

对应的 pipeline、gate 和 condition 位于 `agents/` 的同类目录中。上游技能的
锁定来源见 [`agents/skills/SOURCES.md`](../skills/SOURCES.md)。服务启动时会校验：
路由引用的 pipeline 存在、每个节点的 Skill 已注册、且节点属于该身份的
`skills.enabled` allowlist。节点的 `allowed_tools` 同时必须属于身份的
`tools.enabled` allowlist，且必须由内置 provider 实际注册；任一项缺失都拒绝
启动，不会静默回退到通用 ReAct。

## 最小格式

```yaml
schema: agentsmith.identity/v1
id: legal
name: 法务助手
description: 合同审查与合规问答。

prompt:
  role: 你是 Smith 的法务领域身份。
  instructions: 先说明适用法域和信息不足之处，不把一般信息表述为正式法律意见。

tools:
  enabled: [read_file, web_search]

skills:
  enabled: [contract-review]

routes:
  - id: contract_review
    examples: ["审查这份合同", "找出违约风险"]
    keywords: [合同, 违约, 条款]
    pipeline: legal-contract-review
    priority: 20
```

`pipeline` 是可选的。未匹配任何路由时，该身份会直接使用通用 ReAct 流程。
当身份显式限制 `tools.enabled` 或 `skills.enabled` 时，它们是 allowlist，而
不是“额外添加”的列表。

## 选择规则

- 创建会话时可传 `identity_id`；
- 未指定时，首条消息会在整个目录中按 `examples`、`keywords` 和 `priority`
  选中一个身份，并固定到该会话；
- 后续消息继续使用已固定的身份。若要切换身份，请创建新会话；
- `GET /api/agent/identities` 可列出启动时加载的身份。

要新增财务、法务等领域，只需新增对应 YAML、所引用的 pipeline YAML，以及
需要时的 SKILL.md；无需修改 `task_router.py` 或新增 Agent 实例。
