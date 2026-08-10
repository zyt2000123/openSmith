# 04h · Skill 技能系统

## 本章结论

`engine/skill/` 将一个目录中的 `SKILL.md` 变成可发现、可启用、可执行并可移交上下文的任务 SOP。它只定义单个技能如何进入 Agent 对话；多个技能的顺序、gate 与恢复属于 [04e · Execution](04e-Execution-运行生命周期与管线.md)。

## 总体架构

```mermaid
graph LR
    D[skills/*/SKILL.md] --> L[parse_skill_md]
    L --> R[SkillRegistry]
    R --> E[execute_skill_events]
    E --> W[workflow prompt layers]
    W --> A[ReAct]
    A --> H[handoff / gate feedback]
    H --> O[skill output events]
```

## 目录与职责

| 文件 | 设计职责 |
| --- | --- |
| `loader.py` | 解析 frontmatter 与正文为 `SkillMeta`、`SkillBody` |
| `registry.py` | 扫描目录、跳过无效技能、按名称检索 |
| `settings.py` | 持久化 enabled/disabled 状态 |
| `store.py` | 安全写入、命名校验与技能安装存储 |
| `executor.py` | 为 skill 构造 workflow prompt、交接前序产物并执行事件流 |

## 核心设计

### SKILL.md 是内容契约

一个技能只有目录顶层存在 `SKILL.md` 才算可发现。frontmatter 提供机器可读元数据，正文提供方法论；解析失败的文件被隔离而不是作为半完整 prompt 使用。这样“目录存在”与“能力可用”是不同概念。

### 执行器保留调用链上下文

`execute_skill_events()` 把技能工作流层、前序 workflow outputs、gate feedback 和可选 handoff 组合后调用 ReAct。设计目标是让后续技能得到已提交的交付与失败反馈，而不是靠模型从全部历史中猜上一节点做了什么。

### 启用状态不修改内置内容

settings 把用户选择保存到 Agent 运行目录；skill source 与 runtime enablement 分离。升级内置 skill 时不会覆盖用户的启用选择，禁用技能也不会删除其方法论文件。

## 失败语义与测试

| 情况 | 结果 |
| --- | --- |
| 没有顶层 `SKILL.md` 或解析失败 | 不进入 registry |
| 技能被禁用 | 路由/pipeline 视为不可用，按执行策略 fallback |
| 强制 skill 不存在 | 发出明确错误/事件，不把名称当普通用户文本 |
| 产物过大 | 交接层按 token budget 裁剪，保留后续节点需要的事实 |

## 自测题

1. 为什么 skill enablement 不应写回内置 `SKILL.md`？
2. 为什么后续节点只应接收已提交的前序产物？
