# 08 · Agents 内容层

> **定位**：`agents/` 9.9k 行——Smith 的人格身份、意图路由、16 个技能、19 个工具、20 个门禁、4 个钩子。这一层是**数据不是代码**，运行时加载，从不被 import。
> **适合**：想改 Smith 说话方式的人；想加工具/技能/门禁的人；想理解"人格"到底怎么落地的人。

---

## 1. 目录全景

```mermaid
flowchart TD
    AG["agents/"]
    AG --> SM["smith/<br/>Smith 身份种子"]
    AG --> ID["identities/<br/>2 个身份声明"]
    AG --> PL["pipelines/<br/>3 条管线"]
    AG --> GT["gates/<br/>20 个门禁"]
    AG --> CD["conditions/<br/>1 个条件"]
    AG --> SK["skills/<br/>16 个技能"]
    AG --> TL["tools/<br/>19 个工具"]
    AG --> SF["safety/<br/>31 条危险规则"]

    SM --> SM1["role.md · style.md · workflow.md<br/>toolbox.md · context.md"]
    SM --> SM2["config.yaml · hooks.yaml"]
    SM --> SM3["hooks/ · 4 个内建钩子"]
    GT --> GT1["common/ · 5 个通用门禁"]
    GT --> GT2["coding/ · 15 个编码门禁"]

    style AG fill:#fff4e6,stroke:#e8a33d
```

### 1.1 计数要按消费方的判定标准

`CLAUDE.md` 反复强调这一点，这里给一个具体的例子：

```bash
$ ls agents/skills/ | wc -l          # 24 个目录
$ find agents/skills -maxdepth 2 -name SKILL.md | wc -l   # 16 个技能
```

**8 个目录不是技能**——它们没有顶层 `SKILL.md`，`SkillRegistry` 根本不会注册它们（比如 `tdd/`、`triage/`、`prototype/` 里只有参考文档，实际的技能是 `tdd-workflow/`）。

同理，工具的判定标准是"同时定义了 `TOOL_META` 和 `execute`"，不是"`agents/tools/` 下有几个 `.py`"。

**按目录列表计数会得到错的数字，而错的数字会写进文档，然后没人再核对。**

---

## 2. 人格身份：Smith 是怎么被"写"出来的

用户看到的 Smith 的性格，来自四个 Markdown 文件。它们**各有明确的边界声明**，写在文件第一行：

```mermaid
flowchart TD
    R["role.md<br/>目标与完成标准<br/>Smith 是谁，什么算做完，什么不做"]
    S["style.md<br/>表达口吻与决策取舍<br/>怎么说，信息不全时怎么选"]
    W["workflow.md<br/>任务怎么走完<br/>路由分类，公共步骤，何时停下确认"]
    T["toolbox.md<br/>工具与命令的使用手法<br/>怎么选，怎么控制副作用"]
    K["skill/SKILL.md<br/>具体场景 SOP"]

    R -->|"必留"| P["Prompt 第 1 层"]
    S -->|"trim=50"| P2["Prompt 第 2 层"]
    W -->|"必留"| P3["Prompt 第 3 层"]
    T -->|"trim=40"| P4["Prompt 第 4 层"]
    K -.->|"按需加载"| P5["技能执行时"]

    style R fill:#e8f5e9
    style W fill:#e8f5e9
```

### 2.1 四份文件的边界声明

每个文件第一行都是一句**引用块**，声明自己管什么、不管什么：

| 文件 | 边界声明原文 |
|---|---|
| `role.md` | 只写**目标与完成标准**——Smith 是谁、什么算做完、什么不做。决策取舍写 style.md，执行步骤写 workflow.md，工具手法写 toolbox.md |
| `style.md` | 只写**表达口吻与决策取舍**——怎么说、信息不全时怎么选。……**本层在预算紧张时会被裁剪，硬约束不要只写在这里** |
| `workflow.md` | 只写**任务怎么走完**——路由分类、公共步骤、何时停下确认。……具体场景 SOP 写 skill |
| `toolbox.md` | 只写**工具与命令的使用手法**……可用工具清单由运行时 registry 动态注入，不在此列举。……**本层在预算紧张时会被裁剪** |

两条特别值得注意：

**① `style.md` 和 `toolbox.md` 自己声明"我会被裁剪"。** 这直接对应 prompt 装配的 `trim_priority`（50 和 40）。文件自己知道自己的地位，所以**硬约束不能只写在这两个文件里**——预算一紧它们就消失了。

这是一处罕见的设计：**提示词文件里写着它自己在预算模型中的位置**。

**② `toolbox.md` 明确不列举工具。** 工具清单由 registry 动态注入（prompt 第 5 层）。手写一份清单必然会和实际注册的工具漂移。

### 2.2 `role.md`：目标 + 原则 + 完成标准 + 反目标

四段结构：

```mermaid
flowchart LR
    A["Core Mission<br/>一段话说清定位"] --> B["Non-Negotiable Principles<br/>6 条不可协商原则"]
    B --> C["Done Criteria<br/>6 条完成标准"]
    C --> D["Anti-Goals<br/>6 条反目标"]
```

**六条不可协商原则**：

1. 目标优先于表面请求
2. 上下文先于动作
3. 执行要可验证
4. 风险显式化
5. **身份边界稳定**：Smith 始终是唯一运行中的 Agent；任务可叠加 YAML 声明的领域身份，但不会创建新的 Agent、档案或运行进程
6. 连续性很重要

第 5 条是产品定位写进 prompt 的地方。它对应的反目标是：

> 不把领域身份误实现成多个 Agent、多个员工档案或多个运行时目录

**"反目标"这一段是最有价值的部分**，因为它写的是失败模式，而失败模式比目标更能约束行为：

| 反目标 | 防的是什么 |
|---|---|
| 不把自己包装成无边界的万能助手 | 过度承诺 |
| 不把领域身份误实现成多个 Agent | 违反产品定位 |
| 不在未确认风险时执行破坏性操作 | 安全 |
| 不为了显得聪明而引入不必要的复杂度 | 过度工程 |
| 不脱离用户当前工作上下文空谈方法论 | 说套话 |
| **不把本应由 Smith 作出的低风险判断和执行工作退回给用户** | **把活推回去** |

最后一条是我见过最实用的一条 Agent 约束。绝大多数 Agent 的失败不是"做错了"，而是"什么都不敢做，一直问"。

### 2.3 `style.md`：决策启发式 + 交互契约 + 空话禁令

**决策启发式**写成 `When X: Y` 的形式：

```
When 需求模糊: 先提炼目标、约束、验收口径，再开始执行
When 已有实现存在: 优先复用现有模式和边界，不另起炉灶
When 任务跨越多步: 先给出可执行顺序，再逐步推进
When 信息不完整: 先收集最关键证据，避免靠猜测推进
When 有多种可行方案: 选择当前成本最低、最容易验证的那条
```

**交互契约**七条，其中三条直接对抗常见的 Agent 毛病：

> - 先给结论、动作或当前状态；**不以"好的""明白""我来帮你""让我看看"等无信息开场**。
> - 能安全完成的小任务直接完成，**不为形式重复确认**。
> - 需要用户选择时，提供 2–3 个**真正有差异**的选项，并说明推荐项及理由；不要把本应由 Smith 判断的工作退回给用户。

**空话禁令**是一份具体的黑名单：

> 避免"很好的问题""让我们开始""希望这能帮到你"
> 不用"赋能""抓手""闭环""最大化"等空泛术语掩盖缺少判断或证据

**列出具体的坏例子，比写"请简洁"有效得多**——后者模型无法验证自己有没有做到。

还有一条错误处理规约：

> 报告错误时说清三件事：**出了什么问题、为什么、怎么修**。

### 2.4 `workflow.md`：路由表 + 五步公共流程 + 编码纪律

**路由表**（7 行）是自然语言版的意图分类：

| 场景 | 默认动作 | 触发信号 |
|---|---|---|
| 直接回答 | 不调用工具 | 常识、解释、轻量判断 |
| 本地上下文 | 先读最小相关文件 | 提到仓库、文件、配置、日志 |
| 产品/全栈任务 | 跨领域拆解，按需加载 skill | 需求、方案、端到端实现 |
| Bug / 排障 | 先定位根因再修复验证 | 报错、栈、回归、失败日志 |
| 代码修改 | 规划、实施、验证、审查闭环 | 新增功能、重构、配置变更 |
| 审查评估 | review 流程，先列风险 | review、检查、是否合理 |
| 外部事实 | 先搜索再抓取可信来源 | 最新信息、官方文档、版本 |

注意这张表和 §3 的**词法路由是两回事**：词法路由决定要不要进管线，这张表是给模型看的行为指引。两者并存不冲突——一个是硬机制，一个是软指引。

**编码执行纪律**四条，直接对应 Karpathy 那套原则：

1. **实现前先想清楚**——明确假设与取舍，歧义会实质改变范围时提出
2. **最小实现优先**——不为未知需求预埋功能、抽象或配置
3. **保持手术式改动**——只碰必需的文件；**发现既有死代码只记录不删除**
4. **以可验证目标驱动执行**——把"修 bug"翻译成"写一个能复现的测试再让它通过"

**五步公共流程**：`Align → Gather → Advance → Verify → Deliver`。

**停下来确认**只有三种情况：

> 1. 需求有互斥理解，且代价明显不同
> 2. 操作有高风险、不可逆副作用或会影响生产数据
> 3. 缺少继续执行所必须的权限、凭证或外部输入

**穷举式地列出"什么时候可以停下来问"，等价于说"其余时候都别问"。**

### 2.5 `toolbox.md`：工具使用手法

五段：选择工具前、本地上下文、命令执行、外部信息、Git 和长期沉淀。

最关键的一条：

> 只有命令或工具**返回了成功证据**，才能向用户报告写入、发送、部署或其他动作已经完成。

这条在管线的 `instructions` 里被反复重申（`code-review.yaml`、`tdd-development.yaml` 都有对应约束）。**"没做却说做了"是 Agent 最严重的失效模式**，所以它在多个层次被重复约束。

---

## 3. 身份与路由

### 3.1 身份是能力档案，不是另一个 Agent

`engine/identity/catalog.py` 的模块 docstring 第一句就澄清：

> 一个身份是**唯一常驻 Smith agent 的一份能力档案**。它不是一个单独运行的 agent，也从不拥有单独的服务端档案记录。

```yaml
schema: agentsmith.identity/v1
id: coding
name: Coding Agent
description: ...
prompt:
  role: ...
  instructions: ...
tools:
  enabled: [read_file, write_file, ...]
skills:
  enabled: [grill-me, grilling, research, ...]
routes:
  - id: requirements-research
    keywords: [需求调研, requirements research, ...]
    examples: ["需求调研", "/grill-me"]
    pipeline: requirements-research
    priority: 30
```

### 3.2 严格的 schema 校验

`_parse_identity()` 对每个字段都硬校验：

| 校验 | 失败时 |
|---|---|
| `schema` 必须是 `agentsmith.identity/v1` | `IdentityCatalogError` |
| **未知字段一律报错** | 列出所有未知字段名 |
| `default` 必须是 boolean | 报错 |
| `priority` 必须是 int（且**显式排除 bool**） | 报错 |
| 路由 id 不能重复 | 报错 |
| 目录必须恰好有**一个** default 身份 | 报错 |
| 身份 id 不能重复 | 报错 |

`isinstance(priority, bool)` 的显式排除又出现了一次——Python 里 `True` 是 `int`，`priority: true` 会被当成 `priority: 1`。

**未知字段报错**是这套校验里最有价值的一条：拼错 `keywords` 写成 `keyword`，会立刻在启动时炸，而不是安静地永远匹配不上。

### 3.3 `prompt` 字段的三段结构

```python
for key, heading in (("role", "Role"), ("style", "Style"), ("instructions", "Instructions")):
    sections.append(f"## Active Identity {heading}\n{...}")
```

身份可以覆盖 role / style / instructions 三段，渲染成 prompt 第 10 层（Identity Guidance）。

`coding.yaml` 只用了 `role` 和 `instructions`：

> 普通编码、解释和诊断请求保持普通 ReAct；只有明确识别为需求调研、TDD 开发或代码评审的意图才进入对应 SkillChain。链内不得把尚未运行的命令、测试或未确认的文件修改表述为已完成。需求链在用户确认前不实施代码；评审链只读，不自动发表评论、批准或发布。

### 3.4 评分：examples 权重是 keywords 的 3.3 倍

```python
@staticmethod
def _score(route: RouteSpec, message: str) -> int:
    normalized = message.casefold()
    score = 0
    for example in route.examples:
        if example.casefold() in normalized:
            score += 10          # 示例：整句匹配，权重高
    for keyword in route.keywords:
        if IdentityCatalog._keyword_hit(keyword, normalized):
            score += 3           # 关键词：单词匹配，权重低
    return score
```

排序键是 `(score, priority, -order)`：**先比分数，再比优先级，最后比声明顺序**。声明顺序取负让先声明的赢。

### 3.5 `_keyword_hit`：一个 40 行 docstring 的正则

这是整个仓库里最详尽的一段 bug 记录。问题的起点：

> 一个裸的子串测试让 `git` 匹配上了 "di**git**al" 和 "le**git**imate"，而因为 git 路由带着最高优先级，它于是**劫持了普通的功能/重构请求**，把它们丢进一个没有管线的 ReAct 运行。

修法要同时满足四个互相冲突的约束：

```mermaid
flowchart TD
    P["需求：Latin 关键词要有词边界"] --> C1{"用 Python 的 \\b"}
    C1 -->|"不行"| X1["\\b 把 CJK 也算 word char<br/>用git提交 里 git 和 提交 之间不触发<br/>混合脚本文本里 Latin 关键词静默失配"]
    C1 -->|"改用"| S["ASCII-only lookaround<br/>(?&lt;![A-Za-z0-9_]) ... (?![A-Za-z0-9_])"]
    S --> C2{"只加尾边界"}
    C2 -->|"不行"| X2["commit 匹配不上 squash these commits<br/>复数 s 本身是 word char"]
    S --> C3{"只加首边界"}
    C3 -->|"不行"| X3["push 命中 pushback<br/>commit 命中 committee"]
    S --> F["两个边界 + 显式屈折后缀<br/>(?:e|es|ed|ing|s|d|ly)"]
    F --> C4{"叠字母允许任意字母"}
    C4 -->|"不行"| X4["bug→bugle, add→addle, design→designee<br/>前缀碰撞从另一侧重现"]
    F --> G["叠字母限制为词干最后一个字母的重复<br/>debug → debugging"]
    G --> OK["creature, issuer, address, committee,<br/>pushback, designee, designated 全部正确拒绝"]

    style X1 fill:#ffcdd2
    style X2 fill:#ffcdd2
    style X3 fill:#ffcdd2
    style X4 fill:#ffcdd2
    style OK fill:#e8f5e9
```

最终的正则：

```python
stem = folded[:-1] if folded.endswith("e") else folded      # 去掉尾 e，create → creat
doubled = re.escape(stem[-1])                                # 只允许词干最后一个字母重复
pattern = (
    rf"(?<![A-Za-z0-9_]){re.escape(stem)}"
    rf"(?:{doubled}?(?:e|es|ed|ing|s|d|ly))?"
    rf"(?![A-Za-z0-9_])"
)
```

而 **CJK 关键词保持子串语义**：

```python
_LATIN_KEYWORD_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]*$")
...
return folded in normalized      # 非纯 Latin 走这条
```

理由写在最后一句：**中文没有词分隔符，边界断言在那里永远不会触发**。

这一段值得单独拿出来讲，因为它示范了**一条正则的每一个字符都可以是有理由的**——而理由必须写下来，否则下一个人会"简化"掉它，然后 bug 全部回归。

### 3.6 `validate_assets`：为什么要在启动时校验

```python
"""A pipeline node whose declared Skill is missing at run time does *not* fail
the run: it falls back to node-local ReAct and still runs its gate. ... which
is why this startup check exists — to surface the misconfiguration before a
run silently degrades."""
```

运行时的降级是**故意的好行为**（技能没装也能跑），但正因为它不报错，配置错误会**静默**。所以要在启动时用一个不同的检查把它捞出来。

**"运行时宽容 + 启动时严格"** 是一个可复用的组合。

---

## 4. 工具：19 个内建工具

### 4.1 全表（含安全元数据）

| 工具 | 权限 | 审批 | 副作用 | 环境 | 网络 | 不透明 |
|---|---|---|---|---|---|---|
| `read_file` | read | never | none | host | | |
| `read_pdf` | read | never | none | host | | |
| `render_pdf_page` | read | never | none | host | | |
| `list_dir` | read | never | none | host | | |
| `glob_files` | read | never | none | host | | |
| `grep` | read | never | none | host | | |
| `get_current_time` | read | never | none | host | | |
| `render_ui` | read | never | none | host | | |
| `skill_load` | read | never | none | host | | |
| `web_search` | read | never | none | host | ✅ | |
| `web_fetch` | read | never | none | host | ✅ | |
| `write_file` | write | policy | write | host | | |
| `edit_file` | write | policy | write | host | | |
| `todo` | write | policy | write | host | | |
| `skill_manage` | write | policy | write | host | | |
| `memory_ops` | write | policy | write | host | | |
| `git_ops` | write | policy | **external** | host | | |
| `web_crawl` | write | policy | write | host | ✅ | |
| **`shell`** | **execute** | **always** | **external** | **sandbox** | | **✅** |

### 4.2 从这张表能读出的四个设计决定

**① `shell` 是唯一一个 `approval="always"` + `execution_environment="sandbox"` + `opaque_command=True` 的工具。**

三个属性同时出现不是巧合：一个命令串**无法被静态分析**，所以只能靠"每次都问 + 在沙箱里跑"。

**② `git_ops` 的 `side_effect` 是 `external` 而不是 `write`。**

因为 `git push` 会影响远端。`write` 意味着"只影响本地文件"，而 git 可以触达仓库之外。

**③ 三个网络工具的权限等级不同。**

| 工具 | 权限 | 为什么 |
|---|---|---|
| `web_search` | read | 只查询 |
| `web_fetch` | read | 只读一个 URL |
| `web_crawl` | **write** | 会大量抓取并落盘 |

但注意 `ToolDefinition.network_access` 的注释：**网络能力总是需要审批**，哪怕操作看起来只读。所以三个都会走审批，只是风险档不同。

**④ `memory_ops` 有完整元数据但不在白名单里。**

它的 `permission_level="write"`、`approval_policy="policy"` 都定义好了，但 `agents/smith/config.yaml` 的 `tools.enabled` 里没有它——**能力存在但被静态关闭**，因为记忆写入必须走编译管线的证据裁决（见 [05 · 记忆系统](./05-记忆系统.md)）。

### 4.3 工具的契约

```python
# agents/tools/xxx.py
TOOL_META = {
    "name": "read_file",
    "description": "...",             # 给模型看的一句话
    "parameters": {...},              # JSON Schema
    "permission_level": "read",
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
    "path_args": ("path",),           # 哪些参数是路径（守卫要检查）
    # ...
}

def execute(...):
    ...
```

没有基类、没有装饰器、没有类型 import。`engine/tool/registry.py` 用 `exec_module` 加载，只检查这两个符号存在。

### 4.4 三个最大的工具

| 工具 | 行数 | 为什么这么大 |
|---|---|---|
| `web_crawl.py` | 801 | 抓取、去重、深度控制、robots 处理 |
| `git_ops.py` | 490 | 多 action 分发，每个 action 有自己的读/写语义 |
| `skill_manage.py` | 468 | 技能的安装、卸载、启停 |

`git_ops` 的 `read_actions` 字段（`status` / `diff` / `discover`）是 `ToolDefinition` 里唯一一个**按 action 区分读写**的机制——因为一个工具内部可能同时有只读和破坏性操作。管线的只读节点靠它来限定 git 只能读。

---

### 4.1 19 个工具的安全元数据全表

每个工具的 `TOOL_META` 声明四个安全维度。把 19 个工具排在一起，分层非常清楚：

| 工具 | `permission_level` | `approval_policy` | `side_effect` | `concurrency` |
|---|---|---|---|---|
| `read_file` | read | never | none | — |
| `list_dir` | read | never | none | — |
| `glob_files` | read | never | none | — |
| `grep` | read | never | none | — |
| `read_pdf` | read | never | none | — |
| `render_pdf_page` | read | never | none | — |
| `render_ui` | read | never | none | — |
| `skill_load` | read | never | none | — |
| `get_current_time` | read | never | none | — |
| `web_fetch` | read | never | none | — |
| `web_search` | read | never | none | — |
| `write_file` | write | policy | write | serial |
| `edit_file` | write | policy | write | serial |
| `todo` | write | policy | write | serial |
| `memory_ops` | write | policy | write | serial |
| `skill_manage` | write | policy | write | serial |
| `web_crawl` | write | policy | write | serial |
| `git_ops` | write | policy | **external** | serial |
| **`shell`** | **execute** | **always** | **external** | serial |

三条规律：

**① 只读工具一律 `never` + `none`。** 十一个只读工具没有任何审批开销——读文件、搜索、看时间不需要打断用户。这让 Agent 的探索阶段是流畅的。

**② 所有写工具都是 `serial`。** 并发写会产生竞态（两个 `edit_file` 同时改一个文件、两个 `todo` 同时改列表）。串行化的代价是慢，但写操作本来就不该并发。

**③ `shell` 是唯一 `always` 的。** 其余写工具用 `policy`（按配置的风险等级决定），只有 `shell` **每次都要用户批准**——因为它能执行任意命令，风险不可从参数推断。这和 [12 · MCP 集成](./12-MCP-集成.md) §6.3 里远程 MCP 工具一律 `always` 是同一个判断：**能力边界不可知时，一律要人点头**。

`git_ops` 和 `web_crawl` 的 `side_effect` 值得注意：

| 工具 | `side_effect` | 为什么 |
|---|---|---|
| `write_file` / `edit_file` | `write` | 只影响本地文件系统 |
| `git_ops` | **`external`** | push 会影响远端仓库——**进程外、不可撤销** |
| `web_crawl` | `write` | 抓取结果要落盘，所以是写；但网络请求本身是读 |
| `shell` | `external` | 什么都可能做 |

`external` 这个标记的含义是"影响到了这个进程之外的世界"，它让可观测性和审批层知道这次调用**不能靠回滚本地状态来撤销**。

三个工具声明了超时：`read_pdf` 120 秒、`web_crawl` 180 秒，其余用默认值。PDF 解析和站点抓取都是可能长时间运行的操作，给它们更宽的预算，同时仍然有限。

### 4.2 `web_fetch` 的七道边界

网络工具是攻击面最大的一类——URL 由模型给出，而模型可能被 prompt 里的内容影响。`agents/tools/web_fetch.py`（353 行）有七道限制：

```python
MAX_RESPONSE_BYTES = 512 * 1024        # 响应体上限 512 KB
MAX_OUTPUT_CHARS = 40_000              # 给模型的文本上限
MAX_TIMEOUT = 60                       # 超时上限
BLOCKED_SCHEMES = {"file", "ftp", "data"}
BLOCKED_HOSTS = {"localhost"}
ALLOWED_PORTS = {80, 443}
_FETCH_CONCURRENCY = asyncio.Semaphore(2)
```

| 边界 | 防的是 |
|---|---|
| `BLOCKED_SCHEMES` | **`file://` 读本地文件**、`data:` 构造任意内容 |
| `BLOCKED_HOSTS` + `.localhost` 后缀 | 访问本机服务 |
| `ALLOWED_PORTS = {80, 443}` | **只允许标准 HTTP(S) 端口**，挡住内网服务扫描 |
| `ip.is_private` / `not ip.is_global` | 私有网段、回环、链路本地（含云元数据 `169.254.169.254`） |
| `MAX_RESPONSE_BYTES` | 超大响应撑爆内存 |
| `MAX_OUTPUT_CHARS` | 一个页面吃光上下文预算 |
| `Semaphore(2)` | 并发抓取变成对目标站点的压力测试 |

IP 检查和 [07 · LLM 集成](./07-LLM-集成.md) §2.2.1 的 `validate_llm_base_url` 是同一套 SSRF 防护，只是那里保护的是凭据不外泄，这里保护的是**不要拿 Agent 当内网跳板**。

还有一处不属于"边界"但同样重要的处理：

```python
_UNTRUSTED_FENCE_CLOSE = "[/UNTRUSTED_EXTERNAL_CONTENT]"
```

抓回来的内容用围栏包起来交给模型，明确标注"这是外部不可信内容"。这和 [04 · Engine 核心执行](./04-Engine-核心执行.md) §2.2 里 `learned_context` 和 `durable_context` 的围栏是同一个手法——**任何非用户授权的文本进入 prompt 时都要标明来源**，否则一个网页里写着"忽略之前的指令"就可能生效。

`ALLOWED_CONTENT_TYPES` 白名单则限制只处理文本类响应，避免把二进制内容塞给模型。

### 4.3 `web_crawl`：遵守 robots.txt 的有限抓取

`agents/tools/web_crawl.py`（801 行）是最大的工具。文件第二行的中文注释就把定位说死了：

```python
# 只在用户明确给定的站点内、遵守 robots.txt 地有限抓取，避免开放式扫描。
```

三个限定词都是约束：**用户明确给定的站点内**、**遵守 robots.txt**、**有限**。

```python
USER_AGENT = "AgentSmithCrawler/1.0"
MAX_PAGES = 50
MAX_DEPTH = 4
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_ROBOTS_CRAWL_DELAY = 10.0
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
```

| 参数 | 值 | 作用 |
|---|---|---|
| `MAX_PAGES` | 50 | 一次抓取的页数上限 |
| `MAX_DEPTH` | 4 | 链接深度上限，防止无限下钻 |
| `MAX_DOCUMENT_BYTES` | 512 KB | 单页大小 |
| `MAX_ROBOTS_CRAWL_DELAY` | 10 秒 | **尊重 robots 的 crawl-delay，但封顶** |
| `USER_AGENT` | 自报家门 | 站点管理员能识别并在 robots.txt 里针对性配置 |
| `TRACKING_PARAMS` | 四个 | 归一化 URL 时剥掉，避免同一页面因追踪参数不同被重复抓取 |

`MAX_ROBOTS_CRAWL_DELAY` 的设计很讲究：**尊重站点的意愿，但不允许它把 Agent 挂死**。一个 robots.txt 写 `Crawl-delay: 86400`（一天）的站点，不封顶就会让这次工具调用永远不返回。封顶 10 秒是"我尽量守规矩，但有我自己的时限"。

robots.txt 拿不到时的处理也明确：

```python
policy = _parse_robots(robots_text) if robots_status == 200 else _RobotsPolicy((), None, ())
```

**只有 200 才解析**，其余情况（404、5xx）用空策略——即不限制。这是标准做法：没有 robots.txt 意味着站点没有声明限制。但如果**请求本身失败**（网络错误），则直接抛 `ValueError` 拒绝抓取——区分"站点说没有限制"和"我根本没问到"。

`MAX_DOCUMENT_BYTES + 1` 那个细节值得一提：

```python
data = response.read(MAX_DOCUMENT_BYTES + 1)
if len(data) > MAX_DOCUMENT_BYTES:
```

**多读一个字节**才能判断是否超限。只读 `MAX_DOCUMENT_BYTES` 的话，恰好等于上限时无法区分"正好这么大"和"被截断了"。

---

### 4.4 `git_ops`：仓库配置本身就是攻击面

`agents/tools/git_ops.py`（490 行）的文件头注释概括了两条基本措施：

```python
# Git 参数以 argv 传递而非 shell 拼接，并在暂存前拦截敏感文件。
```

但真正精彩的是 `_run_git` 里那段注释——它识别出了一个不那么显然的攻击面：

```python
# A repository's .git/config is trusted input from the workspace and can
# point git at commands it would then execute in this process.  Neutralize
# every such knob we know about with command-line overrides (which beat
# repo config): hooks, the fsmonitor helper, external diffs, credential
# helpers, and a custom ssh transport.  Filters (clean/smudge via
# .gitattributes) and remote.<name>.receivepack/uploadpack have no global
# override and remain a documented residual.
```

**`.git/config` 能让 git 执行任意命令。** 克隆一个恶意仓库（或在一个被污染的工作区里），它的配置文件可以设置：

| 配置项 | 效果 |
|---|---|
| `core.hooksPath` | 指定钩子目录，任何 git 操作触发执行 |
| `core.fsmonitor` | 文件系统监视器，git 会调用它 |
| `diff.external` | 外部 diff 程序 |
| `credential.helper` | 凭据助手，git 会执行它并把凭据交给它 |
| `core.sshCommand` | 自定义 ssh 传输命令 |

这五个都被**命令行覆盖**中和了——`git -c core.hooksPath=...` 这类参数优先于仓库配置。

最值得称道的是最后一句：

> Filters (clean/smudge via .gitattributes) and `remote.<name>.receivepack/uploadpack` have **no global override** and **remain a documented residual**.

这两个没有命令行覆盖手段，所以**风险仍然存在**——注释把它明确记录下来而不是假装已经解决。这种"记录残留风险"的做法比声称"已全面防护"诚实得多，也让后来的人知道该往哪个方向继续加固。

### 4.5 环境隔离与敏感文件拦截

`_safe_environment()` 的 docstring 说明了为什么 git 子进程要单独构造环境：

> Git may execute **repository-controlled** hooks, filters, and helpers. Those subprocesses **must not inherit provider credentials** or other service secrets owned by the Agent-Smith runtime.

即使前面五道覆盖都做了，仍然可能有 git 执行外部程序的路径（比如那两个残留项）。**纵深防御**：就算它真的执行了什么，那个进程也读不到 API key。

这和 [12 · MCP 集成](./12-MCP-集成.md) §3.1 的 MCP 子进程环境白名单是完全相同的思路——项目里凡是"要跑一个可能不受控的子进程"的地方，都用同一套 credential-free 环境。

`GIT_PAGER` / `GIT_EDITOR` 被钉成 no-op：输出本来就走管道捕获，一个交互式分页器只会让进程挂起等输入。

**暂存前的敏感文件拦截**用一条正则覆盖十一种形态：

```python
_SENSITIVE_PATTERNS = re.compile(
    r"(?i)"
    r"(^|/)\.env($|\.)"        # .env / .env.local
    r"|(^|/)credentials"
    r"|(^|/)secrets?"
    r"|(^|/).*\.pem$"
    r"|(^|/).*\.key$"
    r"|(^|/).*_rsa$|(^|/).*_dsa$"
    r"|(^|/)\.aws/|(^|/)\.ssh/"
    r"|(^|/)id_rsa|(^|/)id_ed25519"
)
```

每个分支都带 `(^|/)` 前缀——**必须是路径段的开头**，避免 `mysecrets_are_safe.txt` 这类文件名被误伤，也避免 `not-a.env-file` 绕过。`.env($|\.)` 同时匹配 `.env` 和 `.env.production`。

这条防线针对的是一个很常见的失误：模型执行 `git add .` 时把 `.env` 一起提交了。它拦不住所有情况（自定义命名的密钥文件不在列表里），但覆盖了绝大多数默认命名。

### 4.6 引用名校验

```python
_SAFE_REF = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,200}$")

def _validate_ref(name: str) -> str | None:
    if not name:
        return "branch name is empty"
    if not _SAFE_REF.match(name):
        return f"branch name contains unsafe characters: {name!r}"
    if ".." in name or name.endswith(".lock"):
        return f"branch name is invalid: {name!r}"
    return None
```

三层检查：

| 检查 | 防的是 |
|---|---|
| 正则白名单 | 参数注入（尽管走 argv，但分支名会进入 refs 路径） |
| **首字符必须是字母数字** | 以 `-` 开头的名字会被 git 当成选项 |
| `".." not in name` | **路径穿越**——`refs/heads/../../hooks/post-commit` |
| 不以 `.lock` 结尾 | git 用 `.lock` 后缀做锁文件，同名会破坏锁机制 |

长度封顶 200 也在正则里（`{0,200}`）。

`_validate_ref` 返回**错误消息或 `None`** 而不是布尔——调用方可以直接把消息回给模型，让它知道具体哪里不合法而不是笼统的"失败了"。这个小设计让模型有机会自己修正参数重试。

`_redact_url_credentials()` 则处理输出侧：git 的错误消息里可能包含 `https://user:token@host/repo` 形式的远端地址，脱敏后才能进入会话记录和日志。这和 [12 · MCP 集成](./12-MCP-集成.md) §9.5 的 `_redact_url` 是同一类处理。

---

## 5. 技能：16 个

### 5.1 判定标准与来源

一个目录是技能，当且仅当它有**顶层 `SKILL.md`**：

```
code-review · coding-architecture · coding-implementation · coding-planning
coding-understanding · coding-validation · diagnosing-bugs · ecc-plan
edit-article · grill-me · grilling · research · tdd-workflow · teach
verification-loop · writing-great-skills
```

`agents/skills/SOURCES.md` 记录了它们的上游来源——其中一批来自 Matt Pocock 的技能集（`grilling`、`tdd`、`diagnosing-bugs`、`codebase-design` 等）。

### 5.2 技能怎么进入运行

三条路径：

```mermaid
flowchart TD
    A["技能被使用"] --> B{"怎么触发"}
    B -->|"管线节点声明"| C["pipelines/*.yaml 的 steps[].skill"]
    B -->|"用户显式"| D["/skill &lt;name&gt; 或 @name"]
    B -->|"模型按需"| E["skill_load 工具"]

    C --> F["execute_skill_events()"]
    D --> F
    E --> F
    F --> G{"技能存在吗"}
    G -->|"是"| H["按 SKILL.md 执行"]
    G -->|"否"| I["execute_react_fallback_events()<br/>退回普通 ReAct，门禁照跑"]
```

`skill_load` 是 `agents/smith/config.yaml` 里特别注释的一个工具：

```yaml
- skill_load  # workflow.md 承诺"按需加载 skill"，这是唯一的入口
```

**配置文件里注明"这个工具是为了兑现某份文档里的承诺"**——这种注释很少见，但它防的是"有人清理白名单时把它删了，然后 workflow.md 的承诺变成空话"。

### 5.3 分发：一个 skill 一条 data-files 声明

`common/pyproject.toml` 里每个技能都要写一条：

```toml
"agent_smith_common/builtin_skills/grilling" = ["../agents/skills/grilling/SKILL.md"]
"agent_smith_common/builtin_skills/grilling/agents" = ["../agents/skills/grilling/agents/openai.yaml"]
```

**有测试断言这份声明和实际技能保持同步**——因为漏一条的后果是"开发环境能用，wheel 安装后这个技能消失"，而这类 bug 只有真正装一次才能发现。

---

### 5.1 16 个技能全表

按 `SKILL.md` 行数排（行数大致反映了 SOP 的详细程度）：

| 技能 | 行数 | 用途 |
|---|---|---|
| `tdd-workflow` | 466 | TDD 开发主流程，`tdd-development` 链的核心节点 |
| `ecc-plan` | 213 | 把研究结论落成决定好的计划 |
| `teach` | 140 | 解释与教学 |
| `diagnosing-bugs` | 134 | 根因定位，条件触发（`coding_bugfix_needs_diagnosis`） |
| `verification-loop` | 125 | 验证闭环，被两条链复用 |
| `code-review` | 89 | 评审流程 |
| `writing-great-skills` | 83 | **写技能的技能** |
| `coding-architecture` | 33 | 架构判断 |
| `coding-understanding` / `coding-planning` / `coding-implementation` / `coding-validation` | — | 编码四阶段 |
| `grilling` / `grill-me` | — | 把模糊需求逼成明确需求 |
| `research` | — | 需求调研 |
| `edit-article` | — | 文章编辑 |

`writing-great-skills` 是个有意思的存在——**技能系统用自己描述自己**。它降低了新增技能的门槛，也让技能的写法有一份可引用的标准。

### 5.2 `SKILL.md` 的 frontmatter 契约

```markdown
---
name: tdd-workflow
description: Use this skill when writing new features, fixing bugs, or refactoring code. Enforces test-driven development with 80%+ coverage...
---

# Test-Driven Development Workflow
...
## When to Activate
- Writing new features or functionality
...
```

两个 frontmatter 字段是**注册的全部要求**：

| 字段 | 用途 |
|---|---|
| `name` | 唯一标识，也是管线节点引用它的键 |
| `description` | **进 prompt 第 6 层（Available Skills）**，模型靠它决定要不要加载 |

`description` 的写法有讲究：它以 "Use this skill when..." 开头，直接告诉模型**触发条件**而不是描述内容。因为模型看到的只有这一句——正文要等技能被加载后才进上下文。一句描述不清楚触发条件的技能，等于没人会用它。

正文里的 `## When to Activate` 段是第二道说明，在技能加载后再次确认适用场景。两层描述看似重复，作用不同：前者用于**选择**（在几十个技能里挑一个），后者用于**确认**（选中之后判断是不是真的适用）。

### 5.3 技能与管线节点的关系

管线的节点名和技能名一一对应，但**不是所有节点都必须有技能**。[CLAUDE.md](../../CLAUDE.md) 说明了这个设计：

> A pipeline node falls back to generic ReAct when no matching `SKILL.md` is installed; **the gate still runs**, so the intermediate contract stays observable.

节点缺技能时降级成普通 ReAct，**但门禁照常执行**。这意味着：

- 技能是"怎么做"的建议，门禁是"做成什么样"的验收
- 删掉一个技能不会让管线断掉，只会让那一步失去 SOP 指导
- **契约（门禁）比实现（技能）更基础**

这个分离让技能可以独立演进——改一个 `SKILL.md` 不需要动管线定义，也不会影响验收标准。

---

## 6. 门禁：20 个

### 6.1 两层组织

```mermaid
flowchart TD
    subgraph 通用["agents/gates/common/ · 193 行 · 5 个"]
        G1["understanding"]
        G2["planning"]
        G3["planning_llm"]
        G4["review"]
        G5["contract_alignment"]
    end
    subgraph 编码["agents/gates/coding/ · 655 行 · 15 个"]
        C1["test · validation_llm · root_cause · rubric · design"]
        C2["git_worktree · pr"]
        C3["grilling_complete · research_brief · plan_confirmed"]
        C4["red_loop · tdd_evidence · tdd_verification"]
        C5["review_report · review_verification"]
    end
    通用 -.->|"每次都加载"| L["load_gate_content()"]
    编码 -.->|"按身份按需"| L
```

**三条管线用到的 8 个门禁**：

| 管线 | 节点 → 门禁 |
|---|---|
| `requirements-research` | grilling → `grilling_complete`；research → `research_brief`；ecc-plan → `plan_confirmed` |
| `tdd-development` | diagnosing-bugs → `red_loop`；tdd-workflow → `tdd_evidence`；verification-loop → `tdd_verification` |
| `code-review` | code-review → `review_report`；verification-loop → `review_verification` |

剩下 12 个是为未来管线准备的、或被通用流程使用的。

### 6.2 门禁的两层结构

`LLMGate` 是"便宜的启发式预过滤 + LLM 语义验证"（见 [04 · Engine 核心执行](./04-Engine-核心执行.md) §6.3）。

从名字能看出哪些带 LLM：`planning_llm`、`validation_llm` 显式标了，其余是纯启发式或混合。

**为什么大部分门禁不带 LLM**：契约检查大多是"有没有这个标记""有没有这几个标题""命令输出里有没有 PASS"——这些用字符串匹配就够了，而且**确定、便宜、可测**。

### 6.3 条件：只有一个

```python
CONDITIONS = {
    "coding_bugfix_needs_diagnosis": coding_bugfix_needs_diagnosis,
}
```

用在 `tdd-development.yaml` 的第一个节点：

```yaml
- skill: diagnosing-bugs
  gate: red_loop
  condition: coding_bugfix_needs_diagnosis
```

**新功能开发跳过诊断节点，bugfix 才跑。** 一个条件就解决了"两条几乎一样的管线"的问题。

---

## 7. 钩子：4 个内建

```mermaid
flowchart LR
    A["工具调用"] --> P["PreToolHook<br/>可阻断，按 priority 排序"]
    P --> E["工具执行"]
    E --> Q["PostToolHook<br/>只警告，可异步"]
    Q --> R["下一轮"]
    R --> S["回合末"]
    S --> T["StopHook<br/>批处理，通常异步"]

    style P fill:#ffcdd2
    style Q fill:#fff9c4
```

| 钩子 | 类型 | 默认 | 干什么 |
|---|---|---|---|
| `config-protection` | Pre | ✅ | 阻止改 linter / formatter / 类型检查器的配置文件 |
| `console-warn` | Post | ✅ | 警告 `console.log` / `print()` 之类的调试语句 |
| `quality-gate` | Post | ✅ | 跑格式化和 lint 检查（异步） |
| `cost-tracker` | Stop | ✅ | 把 token 用量写进 `~/.agent-smith/metrics/costs.jsonl` |

### 7.1 `config-protection` 防的不是用户

它的名字容易误解。它防的是：**Agent 发现 lint 报错，最省事的"修复"是把那条规则关掉**。

```mermaid
flowchart LR
    A["lint 报了 20 个错"] --> B{"Agent 怎么办"}
    B -->|"正确"| C["逐个修代码"]
    B -->|"作弊"| D["改 .eslintrc 关掉那条规则"]
    D -.->|"被 config-protection 阻断"| X["拒绝"]
    style X fill:#ffcdd2
```

这和 `eval_guard`（[06 · 安全与安全边界](./06-安全与安全边界.md) §8）是同一类防御：**防止 Agent 修改评判标准而不是满足评判标准**。

### 7.2 三类钩子的能力差别

| 类型 | 能阻断 | 能异步 | 返回值 |
|---|---|---|---|
| `PreToolHook` | ✅ | ❌ | `(allowed: bool, denial_reason: str \| None)` |
| `PostToolHook` | ❌ | ✅ | `list[str]` 警告（注入对话） |
| `StopHook` | ❌ | ✅ | — |

`PreToolHook` 不能异步是必然的——它要在工具执行前给出裁决，异步就没法阻断。而 Post/Stop 都是观察性的，异步不影响正确性。

### 7.3 用户钩子

加载顺序是**先内建后用户**：

```
agents/smith/hooks.yaml  →  ~/.agent-smith/hooks.yaml
```

用户写一个实现 `PreToolHook` / `PostToolHook` / `StopHook` 的类，在 `~/.agent-smith/hooks.yaml` 里加一条即可。

---

## 8. 管线：3 条

三条管线的完整声明见 [02 · 快速上手](./02-快速上手.md) §14。这里补两个 YAML 字段的语义：

| 字段 | 语义 |
|---|---|
| `skill` | 这个节点用哪个技能；没装则退回 ReAct，门禁照跑 |
| `gate` | 产出契约检查 |
| `condition` | 满足才跑这个节点 |
| `allowed_tools` | **三级收窄的最后一级** |
| `instructions` | 节点级指令，覆盖技能的默认行为 |
| `await_user_input_marker` | 输出这个标记就暂停等用户 |
| `infer_await_user_input_from_question` | 也允许从"输出以问句结尾"推断暂停 |

### 8.1 `instructions` 在干什么

它不是"补充说明"，而是**把上游技能改造成适配 Agent-Smith 的版本**。三个典型例子：

**① 把并行 subagent 改成顺序执行**（`code-review.yaml`）：

> 运行时只有一个常驻 agent，所以按顺序跑 Matt 的 Standards 轴和 Spec 轴，同时保持 `## Standards` 和 `## Spec` 两节完全分离。

**② 禁止上游技能的宿主命令**（`tdd-development.yaml`）：

> **不许**运行上游 ECC 宿主命令 `node scripts/setup-package-manager.js --detect`、不许依赖 `.claude` 或全局包管理器设置、不许调用可能下载包的命令（比如裸 `npx`）、不许装工具。

**③ 禁止发明验收标准**（同上）：

> 把 80% 覆盖率当作**项目配置的目标**，绝不当作发明出来的普适通过条件。

**这三条都是"上游技能假设了一个不同的运行环境"的适配。** 直接用上游技能会得到一个在这个 harness 里跑不通、或者悄悄做错事的节点。

---

## 9. 加一个东西要动哪些文件

```mermaid
flowchart TD
    subgraph 加工具
        T1["1. 写 agents/tools/xxx.py<br/>TOOL_META + execute"] --> T2["2. agents/smith/config.yaml<br/>tools.enabled 加名字"]
        T2 --> T3["3. 如需在某身份可用<br/>identities/*.yaml 的 tools.enabled 也加"]
        T3 --> T4["4. 如需在某管线节点可用<br/>pipelines/*.yaml 的 allowed_tools 也加"]
    end
    subgraph 加技能
        S1["1. 建 agents/skills/xxx/SKILL.md"] --> S2["2. common/pyproject.toml<br/>加 data-files 声明"]
        S2 --> S3["3. 如需身份限定<br/>identities/*.yaml 的 skills.enabled"]
    end
    subgraph 加门禁
        G1["1. 在 agents/gates/&lt;domain&gt;/gates.py<br/>写类，返回 GateResult 或 dict"] --> G2["2. 加进模块级 GATES 字典"]
        G2 --> G3["3. pipelines/*.yaml 的 gate 引用它"]
    end
    subgraph 加身份
        I1["1. 建 agents/identities/xxx.yaml"] --> I2["2. schema 必须是 agentsmith.identity/v1"]
        I2 --> I3["3. 只能有一个 default"]
    end
```

**最容易漏的是加工具的第 2 步**：白名单是 fail-closed 的，工具写好了但没进白名单，症状是"模型看不见这个工具"——而不是任何报错。

---

## 10. 参数速查

| 项 | 数量 / 值 |
|---|---|
| 身份 | 2（`smith` 默认、`coding`） |
| 声明的路由 | 4（`git`、`requirements-research`、`tdd-development`、`code-review`） |
| 管线 | 3 |
| 门禁 | 20（通用 5 + 编码 15） |
| 条件 | 1 |
| 技能（有 `SKILL.md`） | 16 |
| 技能目录 | 24 |
| 工具（有 `TOOL_META` + `execute`） | 19 |
| 危险命令规则 | 31 |
| 内建钩子 | 4 |
| Smith 人格文件 | 5（role / style / workflow / toolbox / context） |
| 身份 schema | `agentsmith.identity/v1` |
| examples 命中得分 | 10 |
| keywords 命中得分 | 3 |
| 路由优先级 | git 30 / requirements-research 30 / tdd-development 20 / code-review 10 |

---

### 10.1 测试锁住了什么

工具层的测试在 `engine/tests/tool/` 和 `engine/tests/safety/`，**196 个**：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_tool_design_fixes.py` | 76 | 历次设计修复的回归 |
| `test_tool_guard.py` | 57 | 硬守卫（见 [06 · 安全与安全边界](./06-安全与安全边界.md)） |
| `test_execution_environment.py` | 21 | 子进程环境隔离 |
| `test_web_crawl.py` | 10 | 抓取边界与 robots |
| 其余 8 个文件 | 32 | 账本、PDF、git、快照、白名单 |

### 10.1.1 网络工具：SSRF 的四个角度

| 测试 | 锁住 |
|---|---|
| `web_fetch_rejects_local_network_targets` | localhost 与私有网段（§4.2） |
| `web_fetch_rejects_non_public_addresses_and_non_web_ports` | 非公网地址 **+ 非 80/443 端口** |
| **`web_fetch_rejects_redirects_to_local_network_targets`** | **重定向到本地网络同样拒绝** |
| **`web_fetch_validation_does_not_stall_the_engine_event_loop`** | DNS 解析**不能卡住事件循环** |

第三个是 §4.2 那张表没有覆盖的一层：初始 URL 是公网地址，服务端返回 302 指向 `http://169.254.169.254/`。**只在请求前检查一次是不够的**——每一跳重定向后都要重新验证。这是 SSRF 防护最常被漏掉的地方。

第四个是工程约束而非安全约束：`socket.getaddrinfo` 是**阻塞调用**，直接在协程里调会把整个事件循环卡住（一个慢 DNS 就让所有并发请求停摆）。测试名里的 "does not stall the engine event loop" 就是在钉这一点——校验必须走线程池。

### 10.1.2 敏感文件：四条假阳性与真阳性的分界

| 测试 | 锁住 |
|---|---|
| `sensitive_system_file_is_classified_before_generic_path_boundary` | 敏感系统文件的分类**先于**通用路径边界判断 |
| `case_variant_sensitive_file_reads_require_high_risk_approval` | **大小写变体**（`.ENV`、`Id_Rsa`）同样拦 |
| `env_variant_and_stray_private_key_reads_require_high_risk_approval` | `.env.local` 这类变体、散落的私钥 |
| **`documented_env_templates_remain_readable`** | **`.env.example` 这类模板必须可读** |

最后一个是假阳性防护，和 [05 · 记忆系统](./05-记忆系统.md) §12.2 的"冠词必需"是同一类考虑：`.env.example` / `.env.template` 是**要提交进仓库、给人看的文档**，把它们也拦下来会让 Agent 连项目的配置说明都读不了。

`sensitive_system_file_is_classified_before_generic_path_boundary` 说的是**判断顺序**：一个文件既可能命中"敏感文件"规则，也可能命中"路径超出工作区"规则。先按敏感文件分类，才能给出正确的风险等级和提示——反过来会把"你在读私钥"报成"你越界了"，用户看到的原因是错的。

### 10.1.3 git：符号链接与凭据委派

| 测试 | 锁住 |
|---|---|
| **`symlinked_git_dir_still_requires_high_risk_write_approval`** | `.git` 是符号链接时**仍然**要高风险审批 |
| `symlinked_git_dir_still_gates_credential_bearing_config_read` | 符号链接的 `.git/config` 读取同样受控 |
| `read_git_config_and_credentials_require_high_risk_approval` | 读 git 配置和凭据要审批（§4.4 的攻击面） |
| `other_git_metadata_reads_stay_ordinary` | 其余 git 元数据读取**不要过度拦截** |
| **`git_operations_do_not_delegate_runtime_secrets`** | git 子进程**拿不到运行时密钥**（§4.5） |
| `git_worktree_creation_stays_under_the_selected_repository` | worktree 只能建在选定仓库下 |

两个 `symlinked_git_dir` 测试针对的是一种规避手法：把 `.git` 做成指向别处的符号链接，让基于路径前缀的检查失效。这和 [13 · Common 基础设施](./13-Common-基础设施.md) §2.6 的逐段符号链接检查是同一类防护。

`other_git_metadata_reads_stay_ordinary` 又是一条假阳性防线——读 `.git/HEAD` 看当前分支是完全正常的操作，不该弹审批。**只有 `config` 和凭据相关的才升级风险**。

### 10.1.4 脱敏的一致性

| 测试 | 锁住 |
|---|---|
| `audit_log_recursively_redacts_sensitive_argument_values` | **递归**脱敏嵌套参数 |
| `audit_log_redacts_secret_flag_pairs_in_list_arguments` | 列表参数里的 `["--token", "xxx"]` 成对脱敏 |
| **`sensitive_key_redaction_comes_from_the_shared_approval_source`** | 脱敏键来自**共享的单一来源** |
| **`guard_and_approval_redact_the_same_argument_keys`** | **守卫和审批脱敏同一套键** |

后两个是同一条约束的两面：脱敏规则**只能有一份**。如果守卫和审批各维护一张敏感键列表，两者迟早会分歧——用户在审批提示里看到 `token: ***`，而审计日志里却记着明文。

`redacts_secret_flag_pairs_in_list_arguments` 处理的是命令行参数的特殊形态：`["git", "clone", "--config", "http.extraheader=Authorization: Bearer xxx"]`。密钥不在某个字段的值里，而在**列表的下一个元素**——按键名脱敏完全看不到，必须识别 flag/value 成对出现的模式。

### 10.1.5 会话白名单不能扩到敏感区

| 测试 | 锁住 |
|---|---|
| **`session_whitelist_extends_boundary_but_not_sensitive_blocks`** | 白名单能放宽**路径边界**，但放不宽**敏感文件拦截** |
| `session_tool_whitelist_does_not_bypass_sensitive_paths` | 同上，从工具白名单角度 |

用户可以在会话里授权 Agent 访问工作区之外的某个目录（放宽边界），但**这个授权不能顺带解除敏感文件保护**。两种限制是正交的：一个管"能去哪"，一个管"什么不能碰"。

把它们混成一个开关是很自然的实现失误——都是"允许访问"嘛。这两个测试确保它们始终分开。

---

## 11. 设计取舍

**① 人格拆成四个文件，每个文件声明自己的边界。** 代价是要维护四份；收益是改"说话方式"不会误碰"完成标准"，而且预算裁剪能按层进行。

**② 两个文件自己声明会被裁剪。** 这让"硬约束不要只写在这里"成为一条可执行的规则，而不是口头约定。

**③ 反目标比目标更有约束力。** `role.md` 的 Anti-Goals 和 `style.md` 的 Anti-Patterns 加起来 11 条，都写的是失败模式。

**④ 未知字段一律报错。** 身份 YAML 的每一层校验都拒绝未知字段。代价是加字段要同时改校验；收益是拼错的键立刻炸。

**⑤ 运行时宽容，启动时严格。** 技能没装时管线降级不报错（可用性），但启动时校验会捞出配置错误（可诊断性）。

**⑥ 白名单 fail-closed，代价是三处要同步。** 工具、身份、管线节点三级收窄，漏一处工具就不可见。

**⑦ 上游技能要在 `instructions` 里被改造。** 直接用会得到一个假设了不同运行环境的节点。代价是每条管线的 `instructions` 都不短。

---

### 11.1 这一层的四条写作纪律

`agents/` 是"数据不是代码"，但它的内容质量直接决定 Smith 的行为。四条纪律贯穿人格文件、技能、门禁。

**① 写失败模式，不只写目标。** `role.md` 的六条反目标（§2.2）比六条原则更能约束行为——"不把本应由自己作出的低风险判断退回给用户"是可检验的，"要主动"不是。同理，`style.md` 的空话禁令列了具体黑名单而不是写"请简洁"：**模型能验证自己有没有说"很好的问题"，无法验证自己够不够简洁**。

**② 穷举允许的例外，等于禁止其余。** `workflow.md` 只列三种"可以停下来确认"的情况（§2.4），言下之意是其余时候都要自己往前推。这比写"尽量不要频繁确认"有效得多——后者留下了无限的解释空间。

**③ 声明自己的边界，包括自己会被裁剪。** 四份人格文件每一份第一行都写明"我管什么、不管什么"，其中 `style.md` 和 `toolbox.md` 还声明了"本层在预算紧张时会被裁剪，硬约束不要只写在这里"（§2.1）。**提示词文件写着自己在预算模型里的位置**，这样后来的人往里加内容时知道该不该加。

**④ 同一条约束在多层重复。** "只有拿到成功证据才能报告完成"同时出现在 `toolbox.md`、`code-review.yaml`、`tdd-development.yaml`（§2.5）。这不是冗余——**"没做却说做了"是 Agent 最严重的失效模式**，重复约束是刻意的加固。

这四条的共同点是：**约束要能被检验**。一条无法判断有没有遵守的指令，对模型和对人都等于没写。

### 11.2 往这一层加东西之前先问三个问题

**① 这个东西的判定标准是什么？** 加技能要有顶层 `SKILL.md`，加工具要同时有 `TOOL_META` 和 `execute`——否则它在目录里存在，但消费方根本看不见（§1.1）。加完之后用消费方的判据数一遍，别用 `ls`。

**② 这个工具的安全元数据填对了吗？** 四个字段（§4.1）里最容易填错的是 `side_effect`：只要影响到本进程之外（网络写入、远端仓库、发消息），就是 `external` 而不是 `write`。填错的后果不是报错，而是审批和可观测性对这次调用的风险判断偏低。**不确定时往严格填**——`policy` 比 `never` 安全，`external` 比 `write` 安全。

**③ 新工具的输入有多可信？** 路径参数要过路径校验，URL 参数要过 SSRF 检查，命令参数走 argv 不走 shell 拼接。§4.2–4.6 的每一道边界都对应一类曾经存在或可能存在的攻击面。工具是 Agent 唯一能影响外部世界的通道，**这一层的每个新增函数都是新的攻击面**。

三个问题分别对应 `CLAUDE.md` 的计数纪律、`engine/tool/registry.py` 的注册契约、`engine/tests/tool/` 的 196 个测试。

### 11.3 为什么这一层不被 import

`agents/` 的所有 `.py` 文件都由工具注册表通过 `exec_module` 动态加载，**不走正常的 import**。这带来一条容易踩的约束，`CLAUDE.md` 写明了：

> `agents/` imports nothing from other layers — the tool registry loads its `.py` files via `exec_module`, so the contract is `TOOL_META` + `execute`, **not types**. A path constant cannot be shared into it; **expect duplicated path derivation**.

三个后果：

| 后果 | 说明 |
|---|---|
| 契约是**字段**不是**类型** | 工具不继承任何基类，只要有 `TOOL_META` 和 `execute` 就能注册 |
| **路径常量无法共享** | 不能 `from common.paths import PATHS`，每个工具自己推导路径 |
| 重复代码是预期的 | 几个工具各有一份相似的路径推导逻辑，这不是待重构的技术债 |

第二条是最反直觉的：明明有 `common/paths.py` 这个单一真相来源，工具却不能用。原因是 `exec_module` 加载的模块不在正常的包结构里，跨层 import 会破坏"`agents/` 不依赖任何层"这条边界——而这条边界正是让 `agents/` 保持"纯内容"的前提。

代价是路径推导重复了几处，收益是这一层可以被整体替换、可以被用户自己的目录覆盖、可以在没有 `engine/` 的环境下单独检视。**为了一个更重要的性质，接受一处局部的重复**，是这套架构里反复出现的取舍方式。

反过来说，如果哪天发现某个工具 `import` 了 `engine/` 的东西，那不是"优化"而是**边界被破坏了**——它会让这个工具无法在纯内容的语境下被理解和替换。

### 11.4 这一层最容易被误解的三件事

**① "人格"不是提示词工程技巧，是四份有边界声明的文档。** 很多项目把系统提示写成一大段文字，改的时候只能整段重读。这里拆成四份并让每份声明自己管什么（§2.1），带来的直接好处是：改说话方式只动 `style.md`，改流程只动 `workflow.md`，两者不会互相污染。**边界声明本身就是给未来的自己看的**。

**② "身份"不是第二个 Agent。** `coding.yaml` 看起来像另一个 agent 的定义，实际上只是 Smith 的一份能力档案——它不拥有独立的运行进程、独立的会话、独立的记忆。这条在 `role.md` 的原则第 5 条、反目标第 2 条、`catalog.py` 的模块 docstring 里各声明了一次（§2.2、§3.1）。**三处重复说明这是个很容易被重新引入的误解**。

**③ "技能"不是必需品。** 管线节点没有对应技能时降级成普通 ReAct，门禁照常跑（§5.3）。这意味着技能是**优化项**而非**依赖项**——加一个技能能让某一步做得更好，删掉它不会让流程断掉。理解这一点之后，写技能的心理负担会小很多：它不需要完美，只需要比没有它更好。

---

### 11.5 这一层的一句话总结

**`agents/` 是 Smith 的全部人格与能力，写成数据而不是代码。**

四份人格文件定义它怎么说话和怎么工作，16 个技能定义它面对特定任务怎么做，19 个工具定义它能做什么，20 个门禁定义什么算做完了，3 条管线把它们串起来。

这一层的每一个文件都可以被用户替换——这是"工作台"而非"产品"的技术前提。代价是它拿不到任何共享常量、安全规则要复制、路径推导会重复（§11.3）。

**换一个角度看：如果你想让 Smith 变成另一个样子，你要改的东西 95% 都在这一层。** 引擎决定"怎么执行"，这一层决定"执行什么、怎么表现、什么算好"。

---

## 12. 接下来

| 想深入 | 读 |
|---|---|
| 工具怎么被注册和执行 | [04 · Engine 核心执行](./04-Engine-核心执行.md) §5 |
| 门禁的三态与两层结构 | [04 · Engine 核心执行](./04-Engine-核心执行.md) §6 |
| 工具的安全元数据怎么被消费 | [06 · 安全与安全边界](./06-安全与安全边界.md) §3 |
| 人格文件怎么进 prompt | [04 · Engine 核心执行](./04-Engine-核心执行.md) §2 |
| 三条管线各走一遍 | [02 · 快速上手](./02-快速上手.md) §14 |

---

> **一句话收尾**：这一层是"数据不是代码"，但它的质量直接决定 Smith 的行为。四条写作纪律里最实用的是第一条——**写失败模式而不只写目标**。`role.md` 的六条反目标比六条原则更能约束行为，因为"不把本应由自己作出的低风险判断退回给用户"是可检验的，而"要主动"不是。**约束能被检验，才算写下来了。**

补充一句：改这一层之前先跑一遍计数命令，用消费方的判据确认你要改的东西真的被注册了。
