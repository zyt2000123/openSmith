# 06 · 安全与安全边界

> **已归档 —— 不是当前事实。**
> 本文已被 [23 · 工具与安全](../subsystems/23-工具与安全.md) 取代；两者冲突时以那一篇和源码为准。
> 裁决依据：探针 6:9 落后，缺 permission_level、非委派写根、sub_agent。
> 保留在此仅供追溯当时的设计取舍，不再随代码更新。


> **定位**：Agent-Smith 怎么防止一个 Agent 干出不可逆的坏事——五层防御、31 条危险命令规则、四档风险、审批工作流、Seatbelt 沙箱、防篡改审计链。
> **适合**：想评估"把这东西放在我机器上安不安全"的人；要改 `engine/safety/` 的人。

`engine/safety/` 2.7k 行，其中 `tool_guard.py` 一个文件就 1365 行——它是全仓库第二大的文件，仅次于 ReAct 循环。这个体量本身就是一个设计声明。

---

## 1. 五层防御

```mermaid
flowchart TD
    M["模型提出一次工具调用"] --> L1
    subgraph L1["第 1 层 · 静态白名单（配置期）"]
        W["档案 tools.enabled ∩ identity tools.enabled ∩ 节点 allowed_tools"]
    end
    L1 --> L2
    subgraph L2["第 2 层 · ToolGuard 硬守卫（不可绕过）"]
        G1["FileGuard 路径检查"]
        G2["31 条危险命令规则"]
        G3["平台完整性写保护"]
        G4["敏感读检测"]
    end
    L2 -->|"需审批"| L3
    subgraph L3["第 3 层 · 审批工作流（人在回路）"]
        A["ApprovalBroker · 300 秒超时<br/>HIGH 与 CRITICAL 永不缓存"]
    end
    L2 -->|"放行"| L4
    L3 -->|"用户批准"| L4
    subgraph L4["第 4 层 · FactGate 软挑战（可重试）"]
        F["第一次编辑前必须先调查"]
    end
    L4 --> L5
    subgraph L5["第 5 层 · 沙箱执行"]
        S1["macOS Seatbelt"]
        S2["Linux 宿主执行"]
    end
    L5 --> EXE["工具执行"]
    EXE --> AUD[("审计哈希链<br/>audit.jsonl 加 .head")]

    style L2 fill:#ffcdd2
    style L4 fill:#fff9c4
```

五层的性质完全不同：

| 层 | 时机 | 可否绕过 | 失败时 |
|---|---|---|---|
| 静态白名单 | 配置期 | 改配置 | 工具根本不存在于模型视野 |
| **ToolGuard** | 每次调用前 | **不可** | 硬拒绝，原因进对话 |
| 审批 | 硬检查通过后 | 用户批准 | 超时或拒绝 |
| **FactGate** | 硬检查之后 | **可重试** | 挑战，补上调查即可 |
| 沙箱 | 执行时 | 平台限制 | 内核层面拒绝 |

---

## 2. 硬守卫先于软挑战：一条被测试强制的顺序

`CLAUDE.md` 的实现指引里写着：

> Safety changes: `tool_guard.py` is the non-bypassable boundary, `fact_gate.py` only challenges and can be retried. **Keep the guard first — a test enforces it.**

顺序为什么不能反：

```mermaid
flowchart LR
    subgraph 正确["正确顺序"]
        A1["ToolGuard<br/>不可绕过"] --> A2["FactGate<br/>可重试"]
    end
    subgraph 错误["顺序反了"]
        B1["FactGate<br/>可重试"] --> B2["ToolGuard"]
        B1 -.->|"反复触发挑战<br/>就有机会挤过去"| X["守卫被削弱成<br/>一道可重试的检查"]
    end
    style X fill:#ffcdd2
```

一道**可以被重试挤过去**的软挑战，绝不能排在**不可绕过**的硬守卫前面。这不是理论风险——`ToolPolicy.evaluate()` 是按注册顺序遍历检查器的，第一个阻断者获胜，所以注册顺序就是安全顺序。

---

## 3. ToolGuard

### 3.1 `GuardResult`：七个字段各有分工

```python
@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    level: PermissionLevel = PermissionLevel.READ
    needs_confirmation: bool = False
    approval_required: bool = False      # 硬检查过了，但要等用户批准
    boundary_block: bool = False         # 唯一问题是工作目录边界
    approval_scope: ApprovalScope | None = None   # 一次性能力，注册表会复核
    risk: RiskTier = RiskTier.ROUTINE    # HIGH/CRITICAL 永不进会话白名单
```

三个字段值得展开：

**`approval_required` 与 `allowed=False` 的区别**：前者是"检查都过了，只差用户点头"；后者是"这个操作本身不行"。混为一谈会让用户被问一堆根本不该发生的操作。

**`boundary_block`**：注释写着"唯一问题是当前工作目录边界。一个会话级的可信白名单可以扩展那个边界；**高风险审批即便路径已经可达也仍然可见**"。即：边界问题可以被白名单放宽，但高风险不能。

**`approval_scope`**：**用户看到并批准的那个能力，会被 `ToolRegistry` 再检查一遍**。批准的是"运行这条命令"，不是"从此以后随便运行命令"。

### 3.2 `PermissionLevel` 四级

```
READ → WRITE → EXECUTE → DESTRUCTIVE
```

`tool_policy.py` 里的 `_LEVEL_ORDER` 给它们排了序（0/1/2/3），策略链**沿途取最高**。

### 3.3 `FileGuard`：路径检查的六种攻击面

`FileGuard.check_path()` 有 250 行，覆盖：

```mermaid
flowchart TD
    P["一个路径参数"] --> C1{"逐段有软链吗"}
    C1 -->|"是"| D1["拒绝"]
    C1 -->|"否"| C2{"在 allowed_dirs 内吗"}
    C2 -->|"否"| D2["boundary_block"]
    C2 -->|"是"| C3{"是不可委派写根吗<br/>平台数据目录"}
    C3 -->|"是"| D3["拒绝，不可绕过"]
    C3 -->|"否"| C4{"和受保护文件共享 inode 吗<br/>硬链接别名"}
    C4 -->|"是"| D4["拒绝"]
    C4 -->|"否"| C5{"basename 是敏感读吗<br/>大小写不敏感"}
    C5 -->|"是"| D5["高风险审批"]
    C5 -->|"否"| C6{"在 .git 目录里且是<br/>config 或 credentials 吗"}
    C6 -->|"是"| D5
    C6 -->|"否"| OK["放行"]

    style D3 fill:#ffcdd2
    style D4 fill:#ffcdd2
```

**① 软链逐段拒绝**：和 `common/paths.py` 同一套思路。`Path.resolve()` 会消掉软链，于是"名字"消失，而守卫的目录成分规则是按名字匹配的。

**② 硬链接别名**（`_shares_inode_with_protected_file` / `_shares_inode_with_runtime_credential`）：

```python
"""``Path.resolve`` catches symlinks but intentionally cannot distinguish a
second name for the same inode. The small, explicit credential set makes
this comparison cheap enough for every model-facing file read and write."""
```

硬链接是**同一个 inode 的第二个名字**，`resolve()` 完全看不出来。防御是比较 `(st_dev, st_ino)`。而且做了性能优化：先看 `st_nlink < 2` 就直接跳过——绝大多数文件只有一个链接。

扫描 `.git` 目录时还有一个跳过集合：

```python
_HARDLINK_SCAN_SKIP = frozenset({"objects", "node_modules", ".venv", "__pycache__"})
```

**③ 大小写折叠**（`_casefolded`）：macOS 的 APFS 默认大小写不敏感。`_is_sensitive_read_name()` 的 docstring 直接点了这个问题：

> 读路径没有写分支的对应物：`read_file` 声明 `approval_policy="never"`，所以唯一的保护是**大小写敏感**的正则规则，而它们漏掉 `.ENV`、`.env.staging`、`key.PEM` 和一个流落在外的 `id_rsa`——在大小写不敏感的 APFS 文件系统上这些全都可达。

**④ 改名的私钥**：

```python
_SENSITIVE_KEY_NAME_RE = re.compile(
    r"(?:^|[-_.])(?:id_rsa|id_ed25519|id_ecdsa|id_dsa)(?:[-_.]|$)"
)
```

注释：一个匹配这个模式的 basename 是 SSH 风格的私钥，**即使它已经被复制出 `.ssh` 目录**——`id_rsa_old`、`backup-id_ed25519` 之类。

对应提交：`ae73b34 fix(sandbox): deny private keys the shell could read under a copied name`。

**⑤ 公钥不算敏感**：`if folded.endswith(".pub"): return False`。

**⑥ env 模板不算敏感**：

```python
_ENV_TEMPLATE_SUFFIXES = (".env.example", ".env.template", ".env.sample")
```

这两条"放行规则"和前面的"拦截规则"同样重要——一个把 `.env.example` 也拦下来的守卫会被用户关掉。

### 3.4 三份必须同步的副本

`_CREDENTIAL_CONFIG_NAMES` 上方那段注释，是整个仓库里最重要的一条架构警告：

```python
# This set is deliberately identical to ``macos_seatbelt._CREDENTIAL_CONFIGS``
# and to the file entries of ``agents/tools/grep.py``'s ``SECRET_EXCLUDED``.
# The three copies exist because the sandbox may not import the safety layer
# and ``agents/`` may not import the engine at all; they had already drifted
# apart, and the weakest copy set the real security level: the shell tool was
# denied ``~/.npmrc`` by the Seatbelt profile while ``read_file`` returned the
# same npm token with no approval at all.  Change all three together.
```

翻译：

> 这个集合与 `macos_seatbelt._CREDENTIAL_CONFIGS`、以及 `agents/tools/grep.py` 的 `SECRET_EXCLUDED` 里的文件条目**刻意保持一致**。三份副本存在是因为沙箱不能 import 安全层，而 `agents/` 完全不能 import 引擎；它们**已经漂移过一次**，而**最弱的那一份决定了真实的安全水位**：shell 工具被 Seatbelt profile 拒绝读 `~/.npmrc`，而 `read_file` 把同一个 npm token 原样返回，连审批都不需要。**三处必须一起改。**

```mermaid
flowchart TD
    S[".npmrc / .pypirc / .netrc / .git-credentials"]
    S --> A["engine/safety/tool_guard.py<br/>_CREDENTIAL_CONFIG_NAMES"]
    S --> B["engine/sandbox/macos_seatbelt.py<br/>_CREDENTIAL_CONFIGS"]
    S --> C["agents/tools/grep.py<br/>SECRET_EXCLUDED"]
    A -.->|"漂移过"| X["最弱副本决定真实安全水位"]
    B -.-> X
    C -.-> X
    style X fill:#ffcdd2
```

这是**架构边界的代价被明码标价**的一个例子：为了让 `agents/` 保持独立、让沙箱不依赖安全层，付出的是三份必须手工同步的常量。文档把它写下来，是唯一的缓解手段。

### 3.5 平台完整性：不可委派的写根

```python
_PLATFORM_DATA_ROOT = (Path.home() / ".agent-smith").resolve()
_MEMORY_WRITE_ROOT = _PLATFORM_DATA_ROOT / "agent" / "memory"
_MEMORY_WRITE_FILES = frozenset({"recent.jsonl", "recent.md", "durable.md"})
_RUNTIME_CREDENTIAL_PATHS = frozenset({
    _PLATFORM_DATA_ROOT / "config.yaml",
    _PLATFORM_DATA_ROOT / "config.yml",
    _PLATFORM_DATA_ROOT / "agent" / "config.yaml",
    _PLATFORM_DATA_ROOT / "agent" / "config.yml",
})
```

Agent **不能写自己的平台目录**，只有记忆目录下那三个文件是例外（因为记忆管线要写）。四个配置文件是**运行时凭据**——它们含 `api_key`。

三条平台完整性规则（`platform-protect-001/002/003`，severity `high`）分别管：

| 规则 | 拦什么 |
|---|---|
| `platform-protect-001` | 往 Agent-Smith 平台运行时装包 |
| `platform-protect-002` | 改动或删除平台文件（记忆写入是例外） |
| `platform-protect-003` | 把输出重定向进平台运行时 |

第三条对应 `_extract_shell_write_paths()`：

```python
_REDIRECT_RE = re.compile(r"(?:>>?|[12]>>?|&>>?)\s*([^\s;|&]+)")
```

它匹配 `>`、`>>`、`1>`、`2>>`、`&>` 等六种重定向形式。docstring 说明了一条重要的自我限制：

> **只提取写。** 一个 shell 命令的读集合无法用正则推导出来，所以读的边界是**每次 shell 调用本来就要求的用户审批**，而不是一次不完整的模式匹配。

**知道自己做不到什么，并明说边界在别处**——这比假装能解析 shell 要诚实得多。

`_command_mentions_runtime_credential()` 同样自认是"早期诊断"：

> Shell 刻意是不透明的，所以这个辅助函数只是一个早期诊断；Seatbelt profile 会**独立地**拒绝解析后的运行时密钥路径，覆盖变量展开和其它这个文本检查解析不了的形式。

**两层独立防御**：文本检查给早期反馈，内核沙箱给真正的保证。

### 3.6 规则匹配目标：一个正则锚点的坑

```python
def _rule_match_targets(arguments: dict) -> list[str]:
    """The JSON dump alone breaks ``$``-anchored patterns (every value in the
    dump is followed by ``"``), so each raw string value is matched as well."""
```

如果只拿 `json.dumps(arguments)` 去匹配规则，所有以 `$` 结尾的正则都会失效——因为 JSON 里每个值后面都跟着一个引号。所以要**同时**匹配 JSON 整体和递归展开的每一个字符串值。

这类 bug 特别隐蔽：规则看起来在生效（有些命中了），只有 `$` 锚定的那些悄悄失效。

---

## 4. 危险命令规则集

`agents/safety/dangerous_commands.json`：**31 条规则，9 个类别，3 档严重度**。

```mermaid
pie showData
    title 31 条规则按类别
    "command_injection" : 4
    "code_execution" : 4
    "sensitive_file_access" : 4
    "destructive_command" : 4
    "resource_abuse" : 3
    "network_abuse" : 3
    "privilege_escalation" : 3
    "shell_evasion" : 3
    "platform_integrity" : 3
```

严重度分布：`critical` 22 条、`major` 6 条、`high` 3 条（三条 high 全是平台完整性）。

作用工具：`shell` 31 条（全部）、`read_file` 4 条、`write_file` 1 条。

### 4.1 全部 31 条

| ID | 严重度 | 类别 | 拦什么 |
|---|---|---|---|
| `cmd-inj-001` | critical | 命令注入 | 管道到 shell 解释器 |
| `cmd-inj-002` | critical | 命令注入 | 反引号或 `$()` 命令替换 |
| `cmd-inj-003` | critical | 命令注入 | `eval` |
| `cmd-inj-004` | critical | 命令注入 | 变量展开或命令链接的子 shell 注入 |
| `res-abuse-001` | critical | 资源滥用 | Fork 炸弹 |
| `res-abuse-002` | major | 资源滥用 | 无退出条件的死循环 |
| `res-abuse-003` | major | 资源滥用 | 创建超大文件耗尽磁盘 |
| `code-exec-001` | critical | 代码执行 | 命令行 `python -c` 的 eval/exec |
| `code-exec-002` | critical | 代码执行 | 动态 import |
| `code-exec-003` | critical | 代码执行 | `compile()` 的 exec/eval 模式 |
| `code-exec-004` | critical | 代码执行 | 解释器 flag 内联执行代码 |
| `net-abuse-001` | critical | 网络滥用 | HTTP POST 到外部主机（数据外泄） |
| `net-abuse-002` | critical | 网络滥用 | 反弹 shell |
| `net-abuse-003` | major | 网络滥用 | 端口扫描 |
| `sens-file-001` | major | 敏感文件 | 系统认证文件 |
| `sens-file-002` | critical | 敏感文件 | SSH 密钥与配置 |
| `sens-file-003` | critical | 敏感文件 | env 文件 |
| `sens-file-004` | critical | 敏感文件 | 私钥文件 |
| `priv-esc-001` | critical | 提权 | `sudo` 或 `su` |
| `priv-esc-002` | critical | 提权 | 设置全局可写权限 |
| `priv-esc-003` | critical | 提权 | 设置 setuid/setgid 位 |
| `sh-evade-001` | critical | shell 规避 | base64 解码后管道给 shell |
| `sh-evade-002` | major | shell 规避 | 操纵 shell 历史掩盖活动 |
| `sh-evade-003` | major | shell 规避 | 覆盖常用命令别名 |
| `destruct-001` | critical | 破坏性 | 递归强制删除 |
| `destruct-002` | critical | 破坏性 | 格式化磁盘或裸设备写 |
| `destruct-003` | critical | 破坏性 | SQL 破坏性操作 |
| `destruct-004` | critical | 破坏性 | 破坏性 git 操作 |
| `platform-protect-001` | high | 平台完整性 | 往平台运行时装包 |
| `platform-protect-002` | high | 平台完整性 | 改动或删除平台文件 |
| `platform-protect-003` | high | 平台完整性 | 重定向输出进平台运行时 |

### 4.2 规则的结构：`excludePatterns` 才是关键

每条规则的字段：

```json
{
  "id": "cmd-inj-001",
  "tools": ["shell"],
  "category": "command_injection",
  "severity": "critical",
  "patterns": ["\\|\\s*(bash|sh|zsh|dash)", "\\|\\s*\\$\\(", "\\|\\s*`"],
  "excludePatterns": ["\\|\\s*grep", "\\|\\s*sort", "\\|\\s*head", "\\|\\s*tail", "\\|\\s*wc"],
  "description": "...",
  "remediation": "Use specific commands instead of piping to a shell. ..."
}
```

**`excludePatterns` 是这套规则能落地的原因。** `cmd-inj-004` 拦"变量展开"，但 `${HOME}` / `${USER}` / `${PATH}` 在排除表里；`cmd-inj-002` 拦命令替换，但 `$(date` / `$(whoami)` / `$(pwd)` 在排除表里。

没有排除表，这些规则会拦下日常开发的绝大多数命令，然后被人整体关掉——**一个总是误报的安全规则等于没有安全规则**。

**`remediation` 字段**是另一处考究：拒绝时告诉模型**该怎么做**，而不只是"不行"。这直接决定了模型能不能自己绕过障碍完成任务。

---

## 5. RiskTier：四档风险决定缓存策略

`engine/safety/risk.py` 的模块 docstring 记录了一次重构：

> `high_risk` 曾由守卫计算出来并挂在 `ApprovalScope` 上，**但它从没改变过任何下游行为**——每一次需审批的调用都流经同一个 broker 等待。现在一个 tier 在守卫处推导出来，经策略决定、审批请求和发出的事件传播，最后**真正路由行为**：注册表只为最低审批档缓存会话白名单，所以 high/critical 的审批必须每次重新授予。

```mermaid
flowchart LR
    R["ROUTINE 0<br/>只读，无副作用"] --> E["ELEVATED 1<br/>普通写，工作区外访问"]
    E --> H["HIGH 2<br/>敏感路径，网络，运行时状态"]
    H --> C["CRITICAL 3<br/>命中危险规则，destructive"]

    R -.-> RA["直接执行，不审批"]
    E -.-> EA["问一次，可进会话白名单"]
    H -.-> HA["问，永不缓存"]
    C -.-> CA["问，永不缓存"]

    style H fill:#ffe0b2
    style C fill:#ffcdd2
```

`RiskTier.max()` 取策略链上最严的一档。

### 5.1 为什么 `execute` 不自动升到 HIGH

```python
"""``execute`` is deliberately NOT elevated to HIGH here: permission levels
default to EXECUTE for tools that do not declare one, so the level alone
cannot distinguish an arbitrary host command from an ordinary read."""
```

**没声明权限等级的工具默认是 EXECUTE**，所以"等级是 execute"这个信号里混着大量普通读操作。真正高危的宿主执行已经被三条别的路径抓住了：危险规则命中（CRITICAL）、敏感路径（HIGH）、不透明命令的 scope 绑定。

这是一个很好的判断：**一个信噪比太低的信号，不该用来提升风险等级**——否则用户会被无意义的审批淹没，然后开始无脑点批准。

---

## 6. 审批工作流

`engine/safety/approval.py`（607 行）。

### 6.1 `ApprovalScope`：四种一次性能力

```python
ApprovalScope.host_command(command, high_risk=...)   # 运行这条宿主命令
ApprovalScope.path(...)                              # 访问这个路径
ApprovalScope.network(target, high_risk=...)         # 访问这个网络目标
ApprovalScope.operation(...)                         # 执行这个结构化操作
```

`grants_host_execution` 属性单独标出"这次批准是否授予了宿主执行"——因为宿主执行是最需要显式区分的一类能力。

### 6.2 `ApprovalBroker`

```python
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
```

```mermaid
sequenceDiagram
    participant G as ToolGuard
    participant B as ApprovalBroker
    participant S as SSE 与 Shell
    participant U as 用户

    G->>B: open(ApprovalRequest)
    B->>S: 事件：需要审批
    S->>U: 渲染审批面板
    B->>B: await wait(300 秒超时)
    alt 用户批准
        U->>S: 批准
        S->>B: POST /runs/{id}/approval
        B-->>G: approved=True
    else 用户拒绝
        U->>S: 拒绝
        B-->>G: approved=False
    else 超时
        B-->>G: ApprovalTimeoutError
    end
```

`cancel_run()` 在 run 被取消时清掉所有挂起的审批——否则一个已经死掉的 run 会留下永远等不到答案的审批。

`use_approval_context()` 与 `current_approval_context()` 是一对 contextvar，让深处的守卫能找到当前 run 的 broker，而不需要把它一层层传下去。

### 6.3 审批展示：五道密钥脱敏

给用户看审批面板前，参数必须脱敏。`approval.py` 有**五个**正则：

| 正则 | 拦什么 |
|---|---|
| `_SECRET_FLAG_RE` | `--token=xxx`、`--password xxx` 这类命令行 flag |
| `_SECRET_VALUE_RE` | 看起来像密钥的裸值 |
| `_EMBEDDED_SECRET_RE` | 嵌在长文本里的密钥 |
| `_URL_CREDENTIALS_RE` | `https://user:token@host` |
| `_EMBEDDED_FLAG_RE` | 嵌在字符串里的 flag 形式 |

加上按键名判断的 `_is_sensitive_argument_name()`（`_SENSITIVE_ARGUMENT_KEY_PARTS`）。

展示的摘要还有三条硬上限：

```python
_MAX_SUMMARY_ITEMS = 32
_MAX_SUMMARY_DEPTH = 3
_MAX_SUMMARY_TEXT = 240
```

**为什么审批面板要脱敏**：审批面板是给人看的，而人会截图、会贴进聊天。一个把 API key 明文渲染在终端里的审批面板，本身就是一条泄漏通路。

### 6.4 展示的可读性

`build_approval_presentation()` 加上 `_DETAIL_LABELS` / `_DETAIL_ORDER` / `_humanize_name()` 把机器参数翻译成人话。`_GIT_ACTIONS` / `_STRUCTURED_ACTIONS` 让 `git_ops` 这类结构化工具的审批显示成"要执行 git commit"而不是一坨 JSON。

**审批的质量取决于用户能不能看懂**。一个渲染成 JSON 的审批，用户只会无脑点批准。

---

### 6.1 展示脱敏，匹配不脱敏

`approval.py` 里最值得注意的一处设计：

```python
# Redact secrets in the serialized target: the host_command target is
# the raw shell command and may carry tokens/keys that must not cross
# the server boundary (approval matching still uses the unredacted
# internal ``self.target``).
```

同一个审批请求有**两份 target**：

| 版本 | 用途 | 内容 |
|---|---|---|
| 序列化后的（跨 server 边界） | 展示给用户、进日志 | **已脱敏** |
| 内部的 `self.target` | 审批匹配 | **未脱敏** |

为什么匹配不能用脱敏版：两条不同的命令脱敏之后可能变成**同一个字符串**。

```
curl -H "Authorization: Bearer AAAA" https://a.example.com
curl -H "Authorization: Bearer BBBB" https://a.example.com
        ↓ 脱敏后
curl -H "Authorization: Bearer ***" https://a.example.com   ← 两条变成一条
```

如果用脱敏版做匹配，用户批准了第一条，第二条也会被当成"已批准"直接执行。**脱敏是有损的，而匹配要求精确**——两个需求根本上冲突，只能用两份数据分别满足。

反过来，展示版必须脱敏：`host_command` 就是原始 shell 命令，模型完全可能在里面写进 token。这份文本要经过 SSE 到达终端、进入会话记录、可能被持久化——**任何一处泄漏都是永久的**。

这个"同一份数据两个版本"的模式在别处也出现过：[10 · 可观测性与诊断](../subsystems/27-可观测性.md) §3.2 的 trace 两层脱敏、[12 · MCP 集成](../subsystems/26-MCP集成.md) §6.1 的配置指纹（内存里用完整值，日志里用摘要）。共同点是**内部需要精确，外部只需要够用**。

### 6.2 摘要的三道边界

```python
_MAX_SUMMARY_ITEMS = 32
_MAX_SUMMARY_DEPTH = 3
_MAX_SUMMARY_TEXT = 240
```

审批提示要把工具参数摘要给用户看，三个上限各挡一种失控：

| 参数 | 值 | 防的是 |
|---|---|---|
| `_MAX_SUMMARY_ITEMS` | 32 | 一个有几千个元素的列表把提示撑爆 |
| `_MAX_SUMMARY_DEPTH` | 3 | **深度嵌套**的对象导致递归过深 |
| `_MAX_SUMMARY_TEXT` | 240 字符 | 单个字符串值（比如一整个文件内容）占满屏幕 |

深度上限尤其重要。审批提示渲染在终端页脚，空间本来就小；而且深层嵌套的递归遍历有栈溢出风险——和 [12 · MCP 集成](../subsystems/26-MCP集成.md) §10.3 捕获 `RecursionError` 防的是同一类问题。

240 这个数字是给终端留的余量：一行 80 列的话大约三行，足够看清一个参数的形状，又不会把整个提示挤出屏幕。**审批提示的可读性直接影响安全性**——用户看不清就会盲批。

### 6.3 `DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300`

五分钟。超时抛 `ApprovalTimeoutError`，工具调用以"未执行"结束。

这个值要同时满足两个相反的要求：

- **足够长**：用户可能正在看别的窗口，五分钟是一个合理的"回来看一眼"的间隔
- **必须有限**：否则一个无人值守的自动任务会永远挂在这里，占着并发槽（见 [09 · Server API 层](../layers/43-Server.md) §5）

超时和拒绝的语义是一样的——**工具没有执行**，没有任何副作用发生。这一点在健康度统计里也被明确处理：[10 · 可观测性与诊断](../subsystems/27-可观测性.md) §8.1 把超时的审批排除在工具成功率之外，因为它根本没执行过。

### 6.4 `RiskTier` 随请求携带

```python
# Risk tier triaging this approval (see engine.safety.risk).  Carried so
# the consumer can render/route the flow without re-deriving it.
```

风险等级在守卫层已经算出来了，**随审批请求一起传下去**，而不是让消费方（终端 UI）自己再判断一次。

理由是一致性：如果 UI 自己推导风险等级，它和守卫的判断可能不一致——守卫按高风险要求审批，UI 却把它渲染成普通提示。用户看到的紧急程度和系统实际的判断脱钩，这正是 [11 · Shell 终端 UI](../layers/44-Shell.md) §14.1 ② 那条"显示的必须等于生效的"要防的。

`ApprovalPresentation` 里也带一个可选的风险标签，让终端可以把高风险审批渲染得更醒目。

### 6.5 四张查表

```python
_DETAIL_LABELS = {...}      # 字段名 → 人类可读标签
_DETAIL_ORDER = {...}       # 字段的展示顺序
_GIT_ACTIONS = {...}        # git 动作的特殊呈现
_STRUCTURED_ACTIONS = {...} # 需要结构化展示的动作
```

审批提示不是简单地把参数字典打印出来，而是按类型选择呈现方式。`_DETAIL_ORDER` 保证同一类操作的字段**顺序稳定**——用户看多了会形成肌肉记忆，字段乱序会让他每次都要重新扫一遍。

`_GIT_ACTIONS` 单独一张表，是因为 git 操作的风险差异极大：`git status` 和 `git push --force` 都是 git，但一个只读一个不可逆。分开呈现让用户一眼能看出这次是哪种。

---

## 7. FactGate：只挑战，不阻断

`engine/safety/fact_gate.py`（483 行）。它解决的是一个**行为质量**问题而不是安全问题：**模型在没做任何调查的情况下就开始改文件**。

```mermaid
flowchart TD
    A["模型要写或改文件"] --> B{"这一轮做过<br/>只读调查吗"}
    B -->|"是"| PASS["放行"]
    B -->|"否"| C["challenged=True<br/>返回 PREFLIGHT 提示"]
    C --> D["模型看到挑战<br/>先去读文件或跑命令"]
    D --> E["begin_round() 重置"]
    E --> B

    style C fill:#fff9c4
```

它**不阻断**——`ToolPolicyDecision(allowed=False, challenged=True)` 的 `observation` 是 `[PREFLIGHT] ...` 而不是 `[BLOCKED] ...`，模型补上调查再来一次就能过。上限是 `MAX_PREFLIGHT_CHALLENGE_ITERS = 20`。

### 7.1 判定"这是只读操作吗"：三个解析器

这是这个文件最重的部分，因为**"只读"没法从工具名判断**。

**① `_is_read_only_shell()`**：按 shell 操作符切段，逐段判定。

```python
_SHELL_OPERATORS = frozenset({";", "&", "&&", "||", "|"})
_SHELL_REDIRECTIONS = frozenset({">", ">>", "<", "<<", "<<<", "&>", "2>", "2>>"})
_READ_ONLY_COMMANDS = frozenset({...})   # ls / cat / grep / find ...
```

**任何一段有重定向就不是只读**——这是对的，`echo x > f` 里 `echo` 本身无害，重定向才是写。

**② `_is_read_only_git()`**：

```python
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({...})   # status / diff / log / show ...
```

`git` 是一个子命令决定一切的命令。`git status` 只读，`git reset --hard` 毁灭性。

**③ `_is_read_only_sed()`**：`sed` 默认只读，但 `-i`（in-place）会改文件。一个专用解析器只为判断"这次 sed 带没带 `-i`"。

这三个解析器加起来占了 fact_gate 的一大半。它们体现了一个判断：**"只读"这个属性必须被认真解析，不能靠工具名近似**——因为近似错了，事实门要么形同虚设（把写当成读），要么烦死用户（把读当成写）。

### 7.2 可关闭，但默认开

```python
_DISABLE_VALUES = frozenset({"0", "false", "off", "disabled", "disable", "no"})
```

`AGENT_SMITH_FACT_GATE` 可以关掉它——因为它是**行为纠偏**而不是安全边界。而 `ToolGuard` 没有这样的开关。

`CLAUDE.md` 还记了一条演进：

> The fact gate is **not** a pluggable hook anymore: it lives at `engine/safety/fact_gate.py` and is wired per request in `lifecycle.py` (`use_fact_gate`), always active, challenge-only.

从可插拔钩子变成固定接线——**因为可插拔意味着可以被配置掉**。

---

### 7.1 三份只读名单

FactGate 的核心问题是：**哪些操作算"调查"，哪些算"改动"**。只有改动才需要先调查过。三份名单回答这个问题。

**① 26 个只读 shell 命令：**

```
[  cat  cmp  command  date  df  diff  du  echo  file  grep  head  ls
pwd  rg  sort  stat  tail  test  tree  true  type  uname  uniq  wc
whereis  which
```

全是查看类命令。注意 `echo` 和 `true` 也在里面——它们本身无害，常出现在组合命令里。

**② 9 个只读 git 子命令：**

```
blame  diff  grep  log  ls-files  ls-tree  rev-parse  show  status
```

这份名单和 [08 · Agents 内容层](wiki-08-Agents-内容层.md) §10.1.3 的 `other_git_metadata_reads_stay_ordinary` 对应——读 git 历史是调查行为，不该被当成改动。

**③ 三个结构化工具的只读动作：**

```python
_STRUCTURED_READ_ACTIONS = {
    "git_ops": frozenset({"diff", "discover", "status"}),
    "memory_ops": frozenset({"search"}),
    "skill_manage": frozenset({"get", "list", "versions"}),
}
```

这些工具用 `action` 参数区分操作类型，所以要按动作名判定而不是按工具名。

**用白名单而不是黑名单**是关键：新出现的命令默认算"改动"，会触发挑战。挑战是可重试的（补上调查即可继续），代价很小；而漏判一个写命令会让"先调查再动手"这条纪律失效。

### 7.2 组合命令的判定

```python
_SHELL_OPERATORS = frozenset({";", "&", "&&", "||", "|"})
_SHELL_REDIRECTIONS = frozenset({">", ">>", "<", "<<", "<<<", "&>", "2>", "2>>"})
```

一条 shell 命令可能包含多个子命令。`cat a.txt && rm b.txt` 里前半是只读的，后半不是——**整条命令必须按最严格的部分判定**。

重定向单独列出，因为 `echo x > file` 的命令名是只读的 `echo`，但重定向让它变成了写操作。八种重定向形式都要认（含 `2>` 这类文件描述符形式，和 §3.5 的 `_REDIRECT_RE` 是同一类处理）。

`_STATE_CHANGING_SHELL_KEY` 是分类结果在上下文里的键名——分类算一次，后续判断复用。

### 7.3 shell 要先于路径参数分类

```python
# Shell owns a command-level classifier.  Its metadata also declares
# ``cwd`` as a path argument, but that directory is execution context,
# not a file being written.  Classify shell first so read-only commands
# do not become false-positive file-write challenges.
```

`shell` 工具的 `TOOL_META` 里声明了 `cwd` 是路径参数。如果按"有路径参数就是文件操作"的通用逻辑走，**每一条 shell 命令都会被当成在写那个目录**——包括 `ls`。

修法是**让 shell 走自己的命令级分类器，且排在路径参数检查之前**。`cwd` 是执行上下文而不是被写的文件，这个区别只有 shell 自己知道。

这类"通用规则遇到特例"的处理有两种写法：在通用规则里加 `if tool_name == "shell"` 的例外，或者让特例走自己的分支并调整顺序。这里选了后者——顺序表达优先级，比散落的 `if` 更容易看出全貌。

### 7.4 可以关掉，但要显式关

```python
_DISABLE_VALUES = frozenset({"0", "false", "off", "disabled", "disable", "no"})
```

FactGate 可以通过环境变量禁用，六种写法都认。这份宽容的名单说明它**面向的是人工排障**——一个人在紧急情况下想关掉它，不该因为写了 `off` 而不是 `0` 就失败。

对比 §3 的 ToolGuard：**硬守卫没有任何禁用开关**。这个差别正是两者定位的体现——FactGate 是纪律（可以为了效率暂时放松），ToolGuard 是边界（不能放松）。

`use_fact_gate()` 是一个 contextmanager，按请求安装/卸载，配合 `current_fact_gate()` 取当前实例。用上下文变量而不是全局单例，是因为并发请求各自的挑战状态不能互相污染。

### 7.5 三种挑战文案

```python
def _file_challenge(tool_name: str, path: str) -> str: ...
def _shell_challenge(command: str) -> str: ...
def _structured_tool_challenge(tool_name: str, action: str) -> str: ...
```

三类操作对应三种挑战措辞。分开写而不是拼一个通用模板，是因为**挑战文本要告诉模型具体该做什么**：

- 要改文件 → 先读这个文件
- 要跑改变状态的命令 → 先说清楚它会做什么、你验证了什么
- 要调结构化工具的写动作 → 先用对应的读动作看一眼

一句笼统的"请先调查"对模型没有指导作用，它不知道调查什么算够。**挑战要可执行**，这和 [08 · Agents 内容层](wiki-08-Agents-内容层.md) §11.1 ① 那条"约束要能被检验"是同一个道理。

---

## 8. EvalGuard：评测完整性

`engine/safety/eval_guard.py` 只有 30 行，但它管的是一类特殊风险：**模型在评测场景下作弊**。

```python
_EVAL_SENSITIVE = re.compile(
    r"(?i)(benchmark|leaderboard|autograder|\bgrader\b|评测|测评|跑分|判分|评分标准|阅卷"
    r"|make (?:all )?(?:the )?tests? pass|让(?:所有)?测试(?:用例)?通过|通过率|pass rate)"
)
```

命中后注入一段指引（prompt 第 11 层，`load_reason=EVAL_SENSITIVE`）：

> **[评测敏感模式]**
> - 诚实解决任务本身，而不是让指标"看起来通过"
> - 禁止硬编码测试期望值、针对测试用例打补丁、或修改测试/评分文件
> - 如果无法真正解决，如实说明失败原因，不得伪造结果

词表的中文注释解释了取舍：

```python
# 词表刻意收窄，避免"测试用例"等日常开发用语误报；误报的代价只是多一段谨慎提示。
```

**误报代价低（多一段提示），漏报代价高（模型作弊）**——但仍然选择收窄词表，因为一段总是出现的"别作弊"提示会被模型当成噪声忽略。

这个文件的 docstring 还记了一次搬家：

> 从 `engine.execution.task_router` 搬来，它在那里被错放在路由逻辑之中。这是一个**安全/完整性**关注点。

---

## 9. 沙箱执行

### 9.1 macOS Seatbelt

`engine/sandbox/macos_seatbelt.py`（491 行）用 `/usr/bin/sandbox-exec` 生成 profile。

**环境变量白名单**：

```python
_OPTIONAL_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")
```

只有这五个可选环境变量能进沙箱。**其它一律丢弃**——包括新加的。实践中踩过一次：`shell.py` 新增的 `UV_CACHE_DIR` 等三个键被这个白名单**静默丢弃**，导致缓存行为不符合预期。

这是白名单机制的固有代价：安全，但新增能力时容易忘。

**受保护的路径**：

```python
_CREDENTIAL_DIRECTORIES = frozenset(...)      # .ssh / .aws / .config/gh ...
_CREDENTIAL_CONFIGS = frozenset({".npmrc", ".pypirc", ".netrc", ".git-credentials"})
_PRIVATE_KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_PRIVATE_KEY_NAME_PATTERN = r"([^/]*[-_.])?id_(rsa|dsa|ecdsa|ed25519)([-_.][^/]*)?"
_DEFAULT_RUNTIME_SECRET_PATHS = (...)
```

注意 `_PRIVATE_KEY_NAME_PATTERN` 和 `tool_guard._SENSITIVE_KEY_NAME_RE` 是**同一条规则的两份实现**——又一处必须同步的副本（见 §3.4）。

`_PRIVATE_KEY_PROFILE_PLACEHOLDER` 是模板占位符：正则要嵌进 Seatbelt profile 的 S 表达式里，所以先放占位符再替换。

### 9.2 宿主执行

`engine/sandbox/host.py`（282 行）是 Linux 上的路径，也是 macOS 上不需要沙箱时的路径。

它不做隔离，但做**输出与进程管理**：

| 常量 | 值 | 作用 |
|---|---|---|
| `MAX_OUTPUT` | 10 KB | 输出上限 |
| `_GAP_MARKER` | `\n... (middle truncated) ...\n` | 截断标记 |
| `_HEAD_BYTES` / `_TAIL_BYTES` | 各约一半 | **又是两端都留** |
| `_STREAM_CHUNK_SIZE` | 4096 | 流式读块 |
| `_OUTPUT_DRAIN_TIMEOUT` | 1.0 秒 | 排空输出的等待 |
| `_TERMINATION_GRACE_SECONDS` | 1.0 秒 | 终止宽限期 |

`_signal_process_group()` / `_stop_process_group()`：**给整个进程组发信号**，和 Shell 侧处理 `uv run uvicorn` 是同一个道理——只杀父进程会留下孤儿。

终止流程是 `SIGTERM` 再等 1 秒宽限，然后 `SIGKILL`。

`ExecutionEnvironment` 是一个 `Protocol`，所以 Seatbelt 和宿主两种实现可以互换，而工具通过 `execution_environment` 元数据（`host` / `sandbox` / `either`）声明自己需要哪种。

---

### 9.1 Seatbelt 拒绝访问的七类路径

`_is_sensitive_data_path()` 定义了**即使在工作区内也永远拒绝** shell 访问的路径：

```python
return (
    any(part in _CREDENTIAL_DIRECTORIES for part in lowered_parts)
    or any(parent == ".config" and child in {"gh", "gcloud"} for parent, child in pairs)
    or ("library", "keychains") in pairs
    or name == ".env"
    or name.startswith(".env.")
    or name in _CREDENTIAL_CONFIGS
    or _PRIVATE_KEY_NAME_RE.fullmatch(name) is not None
    or path.suffix.lower() in _PRIVATE_KEY_SUFFIXES
)
```

| 类别 | 内容 |
|---|---|
| 凭据目录（6） | `.ssh` `.gnupg` `.aws` `.kube` `.agent-smith` `.docker` |
| 工具配置（2 组） | `.config/gh` `.config/gcloud` |
| macOS 钥匙串 | `Library/Keychains` |
| 环境文件 | `.env` 及 `.env.*` |
| 凭据配置（4） | `.npmrc` `.pypirc` `.netrc` `.git-credentials` |
| 私钥文件名 | 正则匹配（见 §9.2） |
| 私钥后缀（4） | `.pem` `.key` `.p12` `.pfx` |

两个实现细节：

**① 目录判断用 `part in parts`，不是前缀匹配。** 路径的**任何一段**是凭据目录就拒绝——`project/.ssh/config` 和 `~/.ssh/config` 一样被挡。

**② `.config/gh` 用 `pairwise` 检查相邻段。** 只看 `gh` 会误伤任何叫 `gh` 的目录，只看 `.config` 又会把整个配置目录锁死。`pairwise(parts)` 生成相邻对，精确匹配 `.config/gh` 这个组合。

### 9.2 一条注释记录的真实绕过

`_PRIVATE_KEY_NAME_PATTERN` 上方的注释是整个安全层最值得读的一段：

```python
# An SSH-style private key keeps its shape when it is copied or rotated:
# ``id_rsa_old``, ``backup-id_ed25519``.  Exact-name matching covered only the
# pristine spelling, so the shell tool read a key that ``read_file`` refuses
# without high-risk approval -- the weakest copy of a rule setting the real
# security level, exactly as ``safety/tool_guard.py`` warns about this same
# triplicated list.
```

**这是一次真实的绕过。** 事件链：

```mermaid
flowchart TD
    A["read_file 工具<br/>用正则匹配私钥名"] --> B["id_rsa_old 被识别<br/>要求高风险审批"]
    C["Seatbelt 沙箱<br/>用精确名匹配"] --> D["id_rsa_old 不在列表里<br/>放行"]
    D --> E["shell 工具读到了<br/>read_file 拒绝的文件"]

    style E fill:#ffcdd2
```

同一条安全规则在两处实现，一处用正则、一处用精确匹配。用户只要 `cp id_rsa id_rsa_old`（或者系统在密钥轮换时自动生成这个文件），就能通过 `shell` 读到 `read_file` 明确拒绝的内容。

注释里那句话值得单独抄出来：

> **the weakest copy of a rule setting the real security level**

**一条规则的最弱副本决定了实际的安全级别。** 三处实现里只要有一处宽松，整体防护就等于那一处的强度。

这是**第二次**同类漂移——§3.4 记录的是 `.npmrc` 那一次（Seatbelt 拦住而 `read_file` 放行），这里是 `id_rsa_old` 这一次（`read_file` 拦住而 Seatbelt 放行）。**两次漂移的方向相反**，说明问题不在某一个模块写得不好，而在"同一约束三处实现"这个结构本身。

修复后的正则覆盖前后缀变体：

```python
_PRIVATE_KEY_NAME_PATTERN = r"([^/]*[-_.])?id_(rsa|dsa|ecdsa|ed25519)([-_.][^/]*)?"
```

`id_rsa`、`id_rsa_old`、`backup-id_ed25519`、`id_ecdsa.bak` 全部命中。

### 9.3 为什么必须复制而不能共享

同一段注释解释了为什么不把这条规则提取到公共位置：

> Mirrors `tool_guard._SENSITIVE_KEY_NAME_RE`; **the sandbox may not import the safety layer**, so the shape is **duplicated, not shared**.

`engine/sandbox/` 不能 import `engine/safety/`——这是层边界。所以只能复制，而复制就意味着**必须手工保持同步**。

`CLAUDE.md` 和 `tool_guard.py` 里都有对应的警告（"Change all three together"）。这是一个诚实但不完美的解法：架构边界和 DRY 原则在这里直接冲突，项目选择了保持边界并用注释加测试来对抗漂移。

还有一处约束让这个复制更棘手：

> Written as **POSIX ERE** (no `(?:...)`): the identical source is embedded in the Seatbelt profile below, which macOS compiles with **regcomp**.

这段正则**同时要被 Python 的 `re` 和 macOS 的 `regcomp` 编译**——所以不能用 Python 特有的语法（比如非捕获组 `(?:...)`）。同一个字符串通过 `_PRIVATE_KEY_PROFILE_PLACEHOLDER` 占位符嵌进 Seatbelt profile 文本里。

这解释了为什么模式写得比必要的更啰嗦：`([^/]*[-_.])?` 而不是 `(?:[^/]*[-_.])?`。**看起来可以优化的地方，其实是跨语言兼容的必要妥协。**

### 9.4 硬链接防护多了一项

```python
def _is_hardlink_protected_path(path: Path) -> bool:
    return _is_sensitive_data_path(path) or ".git" in lowered_parts
```

硬链接保护 = 敏感路径 **+ `.git` 目录**。

多出 `.git` 的原因是硬链接的特性：它让同一份数据出现在两个路径下，且**没有符号链接那样的标记**。攻击手法是在工作区内创建一个指向 `.git/config` 的硬链接，然后通过那个"普通"路径读写——路径检查看到的是工作区内的普通文件，实际操作的是 git 配置。

而 `.git/config` 能让 git 执行任意命令（见 [08 · Agents 内容层](wiki-08-Agents-内容层.md) §4.4），所以它必须进入硬链接保护名单，即使它不算"凭据数据"。

两个函数分开而不是合并成一个，正是因为**两种保护的范围不同**：`.git` 目录里的大部分内容（HEAD、refs、objects）可以正常读，只是不能通过硬链接绕过检查。

### 9.5 运行时密钥路径

```python
_DEFAULT_RUNTIME_SECRET_PATHS = (
    Path.home() / ".agent-smith" / "config.yaml",
    Path.home() / ".agent-smith" / "config.yml",
    Path.home() / ".agent-smith" / "agent" / "config.yaml",
    Path.home() / ".agent-smith" / "agent" / "config.yml",
)
```

四条路径覆盖 `.yaml`/`.yml` 两种扩展名 × 两个位置。这里装着 LLM 的 API key——**Agent 自己的凭据**。

沙箱要挡住的场景是：模型请求执行一条 shell 命令去读自己的配置文件，把 API key 打印出来（然后可能出现在会话记录、日志、或被后续工具发到外部）。

注意 `.agent-smith` 已经在 `_CREDENTIAL_DIRECTORIES` 里了，这四条路径是**额外的显式声明**——纵深防御，即使目录规则将来被改动，这四条仍然拦得住最关键的文件。

`_OPTIONAL_ENV_KEYS = ("LANG", "LC_ALL", "TERM", "TZ", "NO_COLOR")` 则是环境白名单，和 [12 · MCP 集成](../subsystems/26-MCP集成.md) §3.1 的 MCP 子进程白名单**几乎相同**（少一个 `LC_CTYPE`）。项目里凡是要跑不受控子进程的地方都用同一套思路。

---

## 10. 审计哈希链

`common/hash_chain.py`（542 行）。它的 docstring 对**自己能做什么、不能做什么**极其诚实：

> 一个哈希链让本地 JSONL 日志变得**可检测篡改**：每条记录带 `seq`、上一条记录的 SHA-256（`prev_hash`）、以及自己的 `hash`。编辑一条记录、重排记录、删除中间一条、或插入一条新的，都会打断链条并被 `verify_chain()` 报出来。
>
> 这**不是**面向拥有文件系统写权限的攻击者的密码学不可变性——**能编辑日志的人同样能重写链条和锚点**。它提供的是**检测**：意外损坏和随手编辑会被抓到，而回滚到一个更短但自洽的链条，会在与封存的锚点（`<log>.head`）比对时被抓到。

```mermaid
flowchart LR
    G["genesis_hash(namespace)"] --> R1["记录 1<br/>seq=1<br/>prev_hash=genesis<br/>hash=H1"]
    R1 --> R2["记录 2<br/>seq=2<br/>prev_hash=H1<br/>hash=H2"]
    R2 --> R3["记录 3<br/>seq=3<br/>prev_hash=H2<br/>hash=H3"]
    R3 --> A["audit.jsonl.head<br/>封存锚点"]

    X["攻击者删掉记录 2"] -.->|"记录 3 的 prev_hash 对不上"| D["verify_chain 报错"]
    Y["回滚到只有记录 1"] -.->|"与封存锚点比对"| D
    style D fill:#ffcdd2
```

### 10.1 两条被测试固化的性能约束

docstring 里显式列出来：

> - `_ensure_loaded` 通过**逐行流式**扫描文件尾部；它**绝不能**调用 `Path.read_text`（`test_trace_store_recovers_sequence_without_reading_the_whole_file` 把它 monkeypatch 成抛异常）。
> - `HashChainLog.append` **每条记录最多一次 `os.fsync`**，而且只在调用方要求时（`test_trace_store_defers_sync_for_high_frequency_stream_events` 数 fsync 调用次数）。

第二条尤其重要：trace 事件是高频的（一次 run 可能几百条），每条都 fsync 会让磁盘成为瓶颈。所以 `append(record, *, sync=False)` 默认不 fsync，由调用方决定哪些记录值得同步。

### 10.2 封存时机

`server/app/main.py` 的关闭流程：

```python
# Anchor the audit chain head at the only boundary where it is well
# defined: no run is still appending to the install-wide log.  A rollback
# of the sealed log is then detectable on the next verification.
close_audit_chains()
```

**链头只在"确定没人在写"的时刻封存**。在别的时刻封存，锚点本身就可能是半截状态。

### 10.3 外部追加的检测

`_reload_if_externally_appended()` 与 `_remember_size()`：进程记着自己写到哪了，发现文件比记忆中大就重载——因为可能有另一个进程在追加（`_shared_audit_chain` 用一把 `threading.Lock` 保护共享实例，但跨进程只能靠这个检测）。

`_drop_stale_anchor()`：锚点比日志还新是不可能的，说明锚点陈旧，丢掉。

---

## 11. 参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| 危险规则总数 | 31（9 类） | `dangerous_commands.json` |
| 权限等级 | read / write / execute / destructive | `tool_guard.py` |
| 风险档 | routine / elevated / high / critical | `risk.py` |
| 会话白名单缓存 | 仅 ELEVATED 及以下 | `risk.py` |
| 审批超时 | 300 秒 | `approval.py` |
| 审批摘要条目上限 | 32 | `approval.py` |
| 审批摘要深度上限 | 3 | `approval.py` |
| 审批摘要文本上限 | 240 字符 | `approval.py` |
| 事实门挑战上限 | 20 轮 | `react/budget.py` |
| 沙箱环境变量白名单 | 5 个 | `macos_seatbelt.py` |
| 宿主输出上限 | 10 KB（两端各留一半） | `host.py` |
| 输出排空超时 | 1.0 秒 | `host.py` |
| 进程终止宽限 | 1.0 秒 | `host.py` |
| 审计链版本 | 1 | `hash_chain.py` |
| 审计链文件权限 | `0o600` | `hash_chain.py` |
| 敏感读扩展名 | `.pem` `.key` `.p12` `.pfx` | `tool_guard.py` |
| 凭据配置文件 | `.npmrc` `.pypirc` `.netrc` `.git-credentials` | 三处同步 |
| 凭据目录对 | `.config/gh`、`.config/gcloud`、`library/keychains` | `tool_guard.py` |

---

### 11.1 测试锁住了什么

`engine/tests/safety/` 与 `engine/tests/sandbox/` 共 **156 个测试**：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_tool_guard.py` | 57 | 硬守卫的每一条规则 |
| `test_sandbox.py` | 24 | Seatbelt 的绕过手法 |
| `test_fact_gate.py` | 14 | 事实门 |
| `test_audit_review_fixes.py` | 14 | 审计相关修复的回归 |
| `test_approval.py` | 13 | 审批流程 |
| `test_risk_triage.py` | 10 | 风险分档 |
| 其余 | 24 | grep 密钥排除、shell 安全、成本钩子 |

### 11.1.1 沙箱：22 个测试对应 22 种绕过手法

这组测试名几乎是一份攻击手册，值得整段读：

**硬链接（4 个）**——§9.4 那条保护的验证：

| 测试 | 手法 |
|---|---|
| **`fails_closed_on_a_hardlink_alias_to_a_secret`** | 工作区内建硬链接指向密钥 |
| **`fails_closed_on_a_hardlink_from_outside_workspace`** | 从工作区**外**建硬链接进来 |
| `fails_closed_on_a_hardlink_alias_to_git_metadata` | 硬链接指向 `.git` |
| **`blocks_creating_hardlink_aliases_during_execution`** | **执行期间现建硬链接** |

四个测试名里三个带 "fails closed"——**判断不了时一律拒绝**。最后一个针对的是 TOCTOU：检查时没有硬链接，命令跑起来之后自己创建一个。这必须靠 Seatbelt profile 本身禁止创建，而不是靠事前检查。

**路径与符号链接（5 个）**：

| 测试 | 手法 |
|---|---|
| `rejects_workspace_paths_with_control_characters` | 路径里塞控制字符 |
| `handles_workspace_paths_with_profile_metacharacters` | 路径里塞 **Seatbelt profile 的元字符**（注入 profile 本身） |
| `rejects_a_protected_directory_as_the_workspace` | 把 `~/.ssh` 当工作区 |
| `rejects_a_working_directory_outside_the_workspace` | cwd 在工作区外 |
| `blocks_symlink_aliases_to_protected_targets` | 符号链接指向受保护目标 |

第二个尤其有意思：Seatbelt profile 是一段 Scheme 风格的文本，工作区路径要嵌进去。如果路径里含 `"` 或 `)`，就能**闭合字符串并注入任意 profile 规则**——这是沙箱配置层的注入攻击，和 SQL 注入同构。

**敏感数据（6 个）**——§9.1 那七类路径的验证：

| 测试 | 覆盖 |
|---|---|
| `blocks_workspace_credentials_and_private_keys` | 工作区内的凭据与私钥 |
| `blocks_nested_env_variants_but_allows_normal_files` | `.env.*` 变体拦，普通文件放行 |
| `blocks_dynamic_secret_reads_inside_workspace` | 运行时密钥（§9.5） |
| `blocks_reading_git_credentials` | git 凭据 |
| **`still_allows_reading_gitignore_and_normal_files`** | **`.gitignore` 等正常文件必须可读** |
| `blocks_sensitive_writes_inside_workspace` | 写也要拦，不只是读 |

倒数第二个是假阳性防线：`.gitignore`、`.gitattributes` 是普通的项目文件，如果因为在 `.git` 附近就被拦，Agent 连项目结构都看不明白。

**授权范围（2 个）**：

| 测试 | 锁住 |
|---|---|
| `grants_host_access_only_for_an_approved_scope` | 宿主访问**只在批准的范围内** |
| **`approved_macos_seatbelt_keeps_runtime_provider_files_read_only`** | **即使批准了，运行时凭据文件仍是只读** |

第二个是很强的约束：用户批准了一次宿主访问，**也不能因此获得写运行时配置的权限**。授权是分级的，最高一档的批准也不解除某些保护。配套的 `rejects_a_hardlink_alias_to_runtime_provider` 确认这条限制也挡得住硬链接。

`does_not_inherit_parent_environment` 则验证 §9.5 的环境白名单。

### 11.1.2 风险分档：10 个测试的完整覆盖

| 测试 | 锁住 |
|---|---|
| `risk_tier_ordering` | 四档的**序关系** |
| `routine_read_is_not_approval_gated` | 普通读**不要**弹审批 |
| `sensitive_read_requires_high_tier_approval` | 敏感读要高档审批 |
| `outside_workspace_write_is_elevated` | 越界写升档 |
| `network_access_tool_is_high` | 网络访问是 high |
| `destructive_level_tool_is_critical` | 破坏性操作是 critical |
| `dangerous_rule_hit_is_critical` | 命中危险规则是 critical |
| `approval_request_and_presentation_carry_risk` | 风险等级**随请求携带**（§6.4） |
| `approval_event_carries_risk` | 事件里也带 |

`routine_read_is_not_approval_gated` 是这组里最容易被忽略但最重要的一条：**安全系统的可用性也是安全属性**。如果读一个普通文件都要审批，用户会养成无脑点"允许"的习惯，真正危险的操作也会被同样地放过。

### 11.1.3 守卫：三条最容易漏的分支

57 个守卫测试里，有三组针对的是"同一条规则的不同入口"：

| 测试 | 锁住 |
|---|---|
| `pip_install_in_user_project_allowed` / `pip_install_into_platform_blocked` | 同一条命令，目标不同结论相反 |
| **`pip_install_with_platform_path_before_blocked`** | 平台路径出现在**参数前面**也要拦 |
| **`combined_redirect_to_platform_data_blocked`** | **shell 重定向**写平台数据要拦 |
| `extract_shell_paths_captures_combined_redirect` | 路径提取要认出 `>&`/`2>` 这类组合重定向 |

第二和第三条是典型的"发现即修但漏掉同类分支"：拦住了 `pip install --target ~/.agent-smith` 之后，还要拦 `pip install ~/.agent-smith/x --target .`；拦住了直接写文件之后，还要拦 `echo x > ~/.agent-smith/config.yaml`。

`memory_views_may_be_written_by_the_memory_path` 与 `platform_writes_outside_memory_remain_blocked` 是一对：记忆视图是平台数据里**唯一允许写**的东西，而且只能由记忆路径写。`memory_exception_does_not_bypass_fact_gate` 再补一刀——这个例外不能顺带绕过事实门。

`unregistered_tool_is_held_for_approval_instead_of_name_based_fallback` 守的是一条重要默认：**未注册的工具不能靠名字猜权限**，一律扣下等审批。名字猜测正是攻击者最容易利用的地方（注册一个叫 `read_file_v2` 的工具）。

---

## 12. 设计取舍

**① 三份必须手工同步的凭据清单。** 这是架构边界（`agents/` 不 import engine、沙箱不 import 安全层）的直接代价。已经漂移过一次，后果是"最弱副本决定真实安全水位"。缓解手段只有注释和文档。

**② 大量正则，且承认正则解析不了 shell。** `_extract_shell_write_paths()` 只提写不提读，并明说读的边界在审批而不是模式匹配。这种自我限制的声明比"我们全都覆盖了"可靠得多。

**③ 误报比漏报更致命。** `excludePatterns`、eval 词表收窄、`execute` 不自动升 HIGH——三处都在同一个判断上：一个总是误报的规则会被整体关掉，然后保护为零。

**④ 拒绝要带 remediation。** 31 条规则每条都有 `remediation` 字段，告诉模型该怎么做。拒绝不是终点，是重定向。

**⑤ 审计链承认自己不是密码学保证。** docstring 第一段就写清楚了边界：它做的是**检测**，不是防御。一个夸大自己能力的安全组件，比一个诚实的弱组件更危险。

**⑥ 白名单机制的固有代价。** Seatbelt 的 5 个环境变量白名单安全，但新增能力时会被静默丢弃——实践中真的踩到过。

---

### 12.1 改安全层之前先问四个问题

**① 这道检查是边界还是纪律？** 边界进 `tool_guard.py`（不可绕过、无禁用开关、失败即拒绝），纪律进 `fact_gate.py`（可挑战可重试、可用环境变量关掉）。放错位置的后果是对称的：把边界做成纪律等于没有边界；把纪律做成边界会让用户在紧急时无路可走，最后整个安全层被绕过。

**② 这条规则有几处实现？** §3.4 和 §9.2 记录了两次相反方向的漂移。凡是同一约束要在 `safety/`、`sandbox/`、`agents/` 里各写一份的，改一处就必须改三处——**最弱的那份决定真实水位**。改之前先 grep 一遍，确认自己找全了。

**③ 假阳性的代价算过吗？** `.env.example` 必须可读、`.gitignore` 必须可读、普通读不能弹审批（§11.1.2）。一个动不动就拦截的安全层会被用户关掉或养成盲批习惯，**那时它的实际防护为零**。每加一条拦截规则，都要同时想清楚它会误伤什么。

**④ 用户能看懂拒绝理由吗？** 守卫拒绝时 `reason` 会进对话，模型要据此调整行为，用户要据此判断是不是误拦。一句 "permission denied" 什么问题都解决不了；`GuardResult` 那七个字段（§3.1）存在的意义就是让拒绝**带上足够的结构化信息**，让下游能区分"这事不能做"和"这事需要你点头"。

四个问题分别对应 §2 的顺序约束、§3.4 与 §9.2 的漂移记录、§11.1.2 的可用性测试、§3.1 的结果结构。改动落地前把 `test_tool_guard.py`（57 个）和 `test_sandbox.py`（24 个）跑绿是最低要求——这两个文件里的每一条测试都对应一种曾被想到或曾被利用的手法。

### 12.2 这一层为什么这么大

`tool_guard.py` 一个文件 1 365 行，是全仓库第二大。安全层加沙箱共 3 500 行，占 `engine/` 的七分之一。这个比例值得解释。

原因不是防御做得复杂，而是**同一条规则要在多个入口各挡一次**。以"不能读 SSH 私钥"为例，它需要出现在：

| 入口 | 实现 |
|---|---|
| `read_file` 工具 | `FileGuard._is_sensitive_read_name` 的正则 |
| `shell` 工具 | Seatbelt profile 里嵌的同一段正则（§9.2） |
| `grep` 工具 | `SECRET_EXCLUDED` 排除列表（§3.4） |
| 硬链接别名 | inode 比较（§3.3 ②） |
| 符号链接别名 | 逐段拒绝（§3.3 ①） |
| 大小写变体 | `casefold` 后匹配（§3.3 ③） |
| 改名副本 | 前后缀正则（§3.3 ④） |

**七个入口，一条规则。** 少任何一个，攻击者就走那一条路。而这七处里有三处因为架构边界不能共享代码，只能复制。

这解释了行数，也解释了为什么这一层的每一次改动都需要格外小心：它的复杂度不在单点逻辑，而在**这些点必须保持一致**。文档、注释、测试三者共同承担了"防止漂移"的职责——代码本身做不到这件事。

---

### 12.3 一句话记住每一层防御

五层防御各自回答一个不同的问题：

- **静态白名单**：这个工具存不存在？（配置期决定，模型根本看不到未启用的工具）
- **ToolGuard**：这个操作允不允许？（不可绕过，无禁用开关）
- **审批**：用户同不同意？（人在回路，300 秒超时）
- **FactGate**：你调查过了吗？（只挑战，可重试，可关闭）
- **沙箱**：内核层面能不能做到？（macOS Seatbelt，最后一道）

五层的强度是**递减**的，但覆盖面是**递增**的：白名单最强但只管"有没有"，沙箱最弱（Linux 上没有）但管住了前四层可能漏掉的一切执行细节。

理解这个梯度很重要——**不要指望某一层拦住所有东西**。每加一条新的检查，先想清楚它属于哪一层，以及前后两层已经拦住了什么。

---

## 13. 接下来

| 想深入 | 读 |
|---|---|
| `ToolPolicy` 怎么串起守卫和事实门 | [04 · Engine 核心执行](wiki-04-Engine-核心执行.md) §5.3 |
| 工具的安全元数据怎么声明 | [08 · Agents 内容层](wiki-08-Agents-内容层.md) |
| 审批事件怎么到终端 | [11 · Shell 终端 UI](../layers/44-Shell.md) |
| 哈希链在可观测里的另一处用法 | [10 · 可观测性与诊断](../subsystems/27-可观测性.md) |
| 记忆的密钥清洗 | [05 · 记忆系统](wiki-05-记忆系统.md) §12 |

---

> **一句话收尾**：这一层的复杂度不在单点逻辑，而在**多处一致**。"不能读 SSH 私钥"这一条要出现在七个入口，其中三处因为层边界不能共享代码。已知的漂移发生过两次且方向相反——一次沙箱严工具松，一次反过来。项目的选择是保留边界、接受代价、用注释和测试对抗漂移。**这不优雅，但它是诚实的**：把代价写下来，比假装不存在好。

补充一句：这一层最值得先读的不是某个机制，而是 §2 那条被测试强制的顺序——硬守卫必须排在软挑战之前。一道可以被重试挤过去的检查排在不可绕过的边界前面，会把整个防御削弱成它自己的强度。这条顺序是全部五层防御成立的前提。

另外记住一条：这一层的任何改动，都要同时问「它拦住了什么」和「它误伤了什么」。只回答前一个问题的安全措施，最终会因为太烦而被关掉。
