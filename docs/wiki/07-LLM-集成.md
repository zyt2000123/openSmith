# 07 · LLM 集成

> **定位**：`engine/llm/` 3.2k 行——配置怎么解析成一次请求、Provider 适配器怎么写、流式协议怎么归一化、用量怎么记账、怎么录制回放。
> **适合**：要接一个新 provider 的人；调模型成本和延迟的人；被中转站坑过的人。

---

## 1. 全景

```mermaid
flowchart TD
    subgraph 配置层
        CF["config_fields.py<br/>字段声明一次，四处派生"] --> MC["model_config.py<br/>五层合并 + 三条路由 + 超时档"]
    end
    MC --> PC["LLMProviderConfig<br/>已解析的连接配置"]
    PC --> FA["factory.py<br/>ProviderRegistry"]
    FA --> AD1["adapters/openai.py<br/>482 行"]
    FA --> AD2["adapters/anthropic.py<br/>654 行"]
    AD1 --> HT["adapters/_http.py<br/>239 行 · 共享 HTTP"]
    AD2 --> HT
    AD1 --> RT["adapters/_retry.py<br/>45 行 · 瞬时故障"]
    AD2 --> RT
    FA --> CL["client.py<br/>ProviderClient · 261 行"]
    CL --> PORT["LLMPort<br/>执行层唯一认识的接口"]
    CL --> OB["observability.py<br/>每次调用一条 GenerationRecord"]
    CL --> US["usage.py<br/>用量方言归一化"]
    PORT --> RP["replay.py<br/>337 行 · 录制与回放"]

    style PORT fill:#e8f5e9
    style CF fill:#e3f2fd
```

分层的核心是两个 Protocol：

| Protocol | 谁实现 | 谁消费 |
|---|---|---|
| `LLMPort` | `ProviderClient`、`RecordingLLM`、`ReplayLLM`、测试替身 | **执行层**（ReAct 循环、门禁、记忆编译） |
| `ProviderAdapter` | `OpenAIAdapter`、`AnthropicAdapter` | **只有 `ProviderClient`** |

执行层**只认识 `LLMPort`**，完全不知道 provider 的存在。这条边界让"换 provider"和"加录制"变成同一类操作——都是换一个 `LLMPort` 实现。

---

## 2. 配置解析

### 2.1 五层合并

见 [02 · 快速上手](./02-快速上手.md) §3.1 的完整图。这里补三个实现细节。

**① 缓存按文件指纹。**

```python
# Deep-merge of the three static config levels, cached per file fingerprint
# (path + mtime + size).  Config resolution runs on every model route lookup,
# so re-reading unchanged YAML files on each call is wasted disk I/O; edits to
# any level are still picked up because the fingerprint changes.  Environment
# overrides (AGENTSMITH_LLM_*) are the lowest-precedence layer and are part of
# the fingerprint so a change invalidates the cache.
```

**环境变量也参与指纹**——否则改一个环境变量不会让缓存失效。

**② 只有四个键有环境变量入口。**

```python
_ENV_LLM_KEYS = (
    ("AGENTSMITH_LLM_API_KEY", "api_key"),
    ("AGENTSMITH_LLM_BASE_URL", "base_url"),
    ("AGENTSMITH_LLM_MODEL", "model"),
    ("AGENTSMITH_LLM_PROVIDER", "provider"),
)
```

**③ `vendor` 是展示元数据，不进请求。**

```python
# Supplier identity is display metadata only. It deliberately stays out of
# route overrides and adapter construction, but must reach runtime prompt
# metadata for truthful model identity responses.
if "vendor" in llm:
    selected["vendor"] = llm["vendor"]
```

`vendor` 的用处是：用户问"你是什么模型"时，运行时上下文（prompt 第 15 层）里有一个诚实的答案。它不进 adapter 构造，也不进 HTTP 请求。

`engine_runtime.py` 的 `_config_fingerprint()` 在算客户端缓存键时会**显式剔除 `vendor`**：

```python
# Display metadata must never affect client reuse or provider requests.
normalized.pop("vendor", None)
```

否则两个只有 `vendor` 不同的配置会各建一个客户端。

### 2.2 `config_fields.py`：一处声明，六处派生

这是 [02 · 快速上手](./02-快速上手.md) §4.1 讲过的设计，这里补一下它解决的具体故障：

> 同一份字段列表曾经存在于四个并不完全一致的地方……**任何一处漏了一个字段都会静默失败**——一个路由级的值会干脆到不了 adapter。

"静默失败"是关键词。你在 `llm.routes.gate.max_output_tokens` 写了一个值，配置读 API 也能读回来，但 adapter 从没收到过它——因为 `ENGINE_ROUTE_FIELDS` 里漏了这个名字。

三个 flag 编码了投影规则：

| flag | 含义 |
|---|---|
| `secret=True` | 只写不读：接受 patch，读 API 永不返回 |
| `route_scoped=False` | 只能写在顶层，不能按路由覆盖 |
| `engine_projected=False` | 不进 adapter 配置 |

只有两个字段用了非默认值：`api_key`（`secret`）、`vendor`（`route_scoped=False` + `engine_projected=False`）、`timeout_profile`（`engine_projected=False`）。

### 2.2.1 `base_url` 的六道校验

`validate_llm_base_url()` 的 docstring 一句话说清了它为什么存在：

> Validate the **credential-bearing** endpoint used by every LLM entry path.

这个 URL 会**带着 API key** 被请求。填错或被篡改，等于把凭据送给一个任意地址——这是标准的 SSRF 风险，而且这里泄漏的是最敏感的东西。

六道检查按顺序：

```mermaid
flowchart TD
    A["base_url"] --> B{"非空字符串"}
    B -->|"否"| X1["✗ must be a non-empty string"]
    B -->|"是"| C{"scheme == https<br/>且有 hostname"}
    C -->|"否"| X2["✗ must be an HTTPS URL<br/>with a hostname"]
    C -->|"是"| D{"无 userinfo / query / fragment"}
    D -->|"有"| X3["✗ must not contain credentials,<br/>a query, or a fragment"]
    D -->|"无"| E{"hostname 是 localhost<br/>或 *.localhost"}
    E -->|"是"| X4["✗ must not target a<br/>private or local IP"]
    E -->|"否"| F["DNS 解析"]
    F -->|"解析失败"| OK1["✓ 放行<br/>留给请求路径报传输错误"]
    F -->|"解析成功"| G{"所有地址都是<br/>公网地址吗"}
    G -->|"否"| X5["✗ must not target a<br/>private or local IP"]
    G -->|"是"| OK2["✓ 放行"]

    style X4 fill:#ffcdd2
    style X5 fill:#ffcdd2
```

几个细节值得单看：

| 检查 | 防的是 |
|---|---|
| 必须 `https` | 明文 HTTP 会让 API key 在链路上可见 |
| 不能有 userinfo | `https://user:pass@host/` 形式会把凭据写进配置文件和日志 |
| 不能有 query / fragment | 同上，query 里常被塞 token；也避免 base_url 拼接路径时产生歧义 |
| 排除 `localhost` / `*.localhost` | 这两个不一定走 DNS，要单独挡 |
| **解析出的每个地址都必须 `is_global`** | 防止 DNS 指向 `127.0.0.1`、`10.x`、`169.254.169.254`（云元数据服务） |

最后一条是核心。`ipaddress.ip_address(...).is_global` 一次性排除了私有网段、回环、链路本地、保留地址——比手写网段列表可靠得多。检查的是**所有**解析结果（`getaddrinfo` 可能返回多个），任何一个不是公网就拒绝。

`hostname.rstrip(".")` 处理的是 FQDN 的尾点（`example.com.` 和 `example.com` 等价），不处理会让 `localhost.` 绕过上面那条字符串检查。

**解析失败为什么放行？**

```python
except socket.gaierror:
    # Leave unresolved public hostnames to the request path, which can
    # report a useful transport error. Any address that does resolve must
    # be public before credentials can be sent to it.
    return base_url
```

DNS 暂时不可用、或者主机名确实不存在——这时拒绝会让一个"网络还没连上"的场景表现为"配置非法"，用户会去改配置而不是查网络。放行之后请求路径会给出准确的传输错误。

安全性上这不是缺口：解析不了的地址也连不上，凭据发不出去。**注释里那句 "Any address that does resolve must be public" 划的就是这条界**——能解析的必须是公网，解析不了的交给下一层。

### 2.2.2 配置缓存按文件指纹

```python
# Deep-merge of the three static config levels, cached per file fingerprint
# (path + mtime + size).  Config resolution runs on every model route lookup,
# so re-reading unchanged YAML files on each call is wasted disk I/O; edits to
# any level are still picked up because the fingerprint changes.  Environment
# overrides (AGENTSMITH_LLM_*) are the lowest-precedence layer and are part of
# the fingerprint so a change invalidates the cache.
```

配置解析发生在**每一次模型路由查找**上——频率很高。每次都重读三个 YAML 文件是纯粹的浪费。

缓存键是 `(路径, mtime, 大小)` 三元组，加上环境变量层。四件事因此同时成立：

| | 效果 |
|---|---|
| 文件没变 | 直接用缓存，零磁盘 I/O |
| 改了任一层 YAML | mtime 变 → 指纹变 → 缓存失效 → 重新合并 |
| 改了 `AGENTSMITH_LLM_*` | 环境变量在指纹里 → 同样失效 |
| 不需要手动清缓存 | 没有 `reload()` 这类 API，也就不存在忘记调用的问题 |

用 mtime + size 而不是内容哈希，是因为读内容算哈希就已经付出了想省掉的那次 I/O。这个组合会漏掉"同一秒内改动且大小不变"的极端情况，但对配置文件来说这个概率可以忽略——而且 `stat` 比 `read` 便宜一个数量级。

缓存键里还加了一样东西：

```python
# The loader identity keeps the cache honest when tests replace
# load_yaml with a stub; the file fingerprints catch real edits.
```

**加载器函数本身的标识**也进缓存键。测试常把 `load_yaml` 换成 stub，如果只按文件指纹缓存，换了 stub 之后仍会命中上一次真实读取的结果，测试就测不到 stub 的行为了。

`_config_paths()` 则在**调用时**才去取 `common.config.PATHS`：

```python
# Optional test overrides.  Production resolution deliberately goes through
# ``common.config.PATHS`` at call time so ``reset_paths()`` takes effect even
# after this module has already been imported.
```

这正是 [13 · Common 基础设施](./13-Common-基础设施.md) §5.5 那条纪律的一个消费方实例——不能在模块级 `from common.config import PATHS`，否则 `reset_paths()` 对这个模块无效。对应的测试 `llm_config_resolves_paths_when_it_runs` 就守在这里。

### 2.3 三条路由与超时档

见 [02 · 快速上手](./02-快速上手.md) §4.2/§4.4。补一条：`LLMTimeouts` 把五个字段翻译成 **两个** `httpx.Timeout`：

```python
def request_timeout(self) -> httpx.Timeout:
    return httpx.Timeout(connect=..., read=self.read, write=..., pool=...)

def stream_timeout(self) -> httpx.Timeout:
    return httpx.Timeout(connect=..., read=self.stream_read, write=..., pool=...)
```

**流式和非流式用不同的 read 超时**。流式的 `read` 语义是"两个 chunk 之间的最大间隔"，非流式是"整个响应的最大等待"——同一个数字对两者意义完全不同。

---

### 2.4 两个协议的精确分工

`LLMPort`（44 行）和 `ProviderAdapter`（27 行）都是 `Protocol`，都带 `@runtime_checkable`。它们的差别值得逐字段对比，因为**哪个字段出现在哪一侧**就是这一层的分层依据。

| | `LLMPort`（对执行层） | `ProviderAdapter`（对 provider） |
|---|---|---|
| docstring | "consumed by **execution code and tests**" | "provider-**specific** implementations" |
| 身份字段 | `model: str` | `provider: str` |
| 入参形状 | `messages` + `tools` 两个裸参数 | 一个 `LLMRequest` 对象 |
| 方法 | `chat` / `chat_events` / `chat_stream` / `close` | `complete` / `stream_response` / `close` |
| `stream: bool` | **有** | 没有 |
| `limits: ModelLimits` | **有** | 没有 |
| `max_output_tokens` | `int` | `int \| None` |

三处差别各有理由：

**① `stream` 只在 `LLMPort` 上。** 执行层需要知道"这次要不要走流式"，而适配器**两种都要实现**——它不做选择，选择在 `ProviderClient` 那一层完成。把 `stream` 放进适配器协议会让每个适配器都要处理一个和自己无关的开关。

**② `limits` 只在 `LLMPort` 上。** `ModelLimits` 是把四个容量字段（窗口、最大输出，各带一个 `_declared`）打包成的值对象，方便执行层一次性传给预算计算。适配器侧保留四个散字段，因为它只负责**上报**自己知道的事实，不负责打包。

**③ `max_output_tokens` 在适配器侧可以是 `None`。** 这是 `*_declared` 布尔之外的第二层表达："我不知道"。到了 `LLMPort` 侧它必须是 `int`——执行层需要一个能直接参与算术的数，`None` 已经被 `ProviderClient` 用默认值填上了，同时把 `max_output_tokens_declared` 设成 `False`。

**入参形状的差别最能说明分层**：

```mermaid
flowchart LR
    A["执行层"] -->|"messages, tools<br/>裸参数"| B["LLMPort"]
    B --> C["ProviderClient"]
    C -->|"LLMRequest<br/>值对象"| D["ProviderAdapter"]
    D -->|"provider 特定 JSON"| E["HTTP"]

    style C fill:#fff3cd
```

执行层传的是"对话内容"，适配器收的是"一次请求的完整描述"——中间那次转换（组装 `LLMRequest`）正是 `ProviderClient` 的核心职责之一：它把温度、超时、输出上限、缓存提示这些**配置来的参数**合并进去，让适配器只关心怎么把一个完整的请求翻译成 provider 的线格式。

反过来说，如果适配器也接 `messages, tools`，那么每个适配器都要自己去读配置——三个适配器就有三处配置读取，参数默认值迟早会不一致。

`chat_stream(messages) -> AsyncIterator[str]` 是 `LLMPort` 上第三个方法，只吐纯文本、不接 `tools`。它服务于不需要工具调用的简单场景（比如后台摘要），适配器侧没有对应方法——由 `ProviderClient` 在 `stream_response` 之上过滤出文本 delta 得到。

`@runtime_checkable` 让 `isinstance(x, LLMPort)` 可用，但要注意 Python 的 `Protocol` 只检查**方法是否存在**，不检查签名。所以它能挡住"完全不像"的对象，挡不住"方法名对但参数不对"的——真正的约束还是靠测试（§18）。

**用 `Protocol` 而不是抽象基类**，是为了让 `RecordingLLM`、`ReplayLLM` 这类包装器不必继承任何东西就能顶替真实客户端（§12）。结构化子类型（鸭子类型的静态版本）在这里比名义继承更合适：包装器和真实适配器之间没有 is-a 关系，只有"能被同样地使用"这层关系。测试里的假客户端同理——写一个只实现三个方法的小类就能用，不需要去继承一个带十几个字段的基类。

代价是失去了基类能提供的默认实现和共享逻辑。这一层用另一种方式补上：`HTTPAdapterMixin`（§7）以 mixin 形式提供两个 HTTP 适配器的公共部分，`ProviderClient` 则承担了从 `ProviderAdapter` 到 `LLMPort` 的适配。**协议定契约，mixin 供复用**，两者分开——这样一个不走 HTTP 的适配器（比如将来接本地模型）可以只实现协议而不用 mixin。

---

## 3. Provider 注册表

`engine/llm/factory.py` 的 `ProviderRegistry` 只有 94 行，但它的每条校验都有理由：

```python
def register(self, provider, builder, *, aliases=()):
    if canonical in self._builders:
        raise ValueError(f"LLM provider is already registered: {canonical}")
    ...
    if normalized_alias in self._aliases:
        raise ValueError(f"LLM provider alias is already registered: {normalized_alias}")
```

**重复注册硬失败**——一个被静默覆盖的 provider 会让"我明明配了 X 为什么走了 Y"变成无解的问题。

```python
def normalize(self, provider: object) -> str:
    if provider is None or (isinstance(provider, str) and not provider.strip()):
        return "openai"        # 空值默认 openai
    if not isinstance(provider, str):
        raise UnsupportedProviderError("LLM provider must be a string.")
    ...
    raise UnsupportedProviderError(
        f"Unsupported LLM provider {provider!r}; supported providers: {supported}."
    )
```

**错误信息里列出支持的名字**——用户拼错 `anthropics` 时，报错直接告诉他有哪些选项。

名称清洗：`strip().lower().replace("-", "_")`。所以 `OpenAI`、`open-ai`、`OPENAI` 都归一到 `openai`。

当前注册表：

| 规范名 | 别名 | 适配器 |
|---|---|---|
| `openai` | `openai_compatible` | `OpenAIAdapter`（482 行） |
| `anthropic` | — | `AnthropicAdapter`（654 行） |

`openai_compatible` 这个别名是给中转站用的——语义上"我不是 OpenAI，但我说 OpenAI 的协议"。

---

## 4. 归一化契约

执行层只认识 `engine/llm/contracts.py` 里的这些类型：

```mermaid
classDiagram
    class LLMRequest {
        +list messages
        +list tools
        +str prefix_cache_key
    }
    class ChatResponse {
        +str text
        +str reasoning
        +list~ToolCallData~ tool_calls
        +dict usage
        +str finish_reason
        +str raw_finish_reason
        +str model
        +has_tool_calls
    }
    class ModelLimits {
        +int context_window
        +bool context_window_declared
        +int max_output_tokens
        +bool max_output_tokens_declared
    }
    class ProviderCapabilities {
        +bool streaming
        +bool tool_calls
        +bool prefix_cache_key
    }
    class LLMTimeouts {
        +float connect
        +float read
        +float stream_read
        +float write
        +float pool
    }
    LLMRequest ..> ChatResponse
    ChatResponse ..> ModelLimits
```

### 4.1 `finish_reason` 与 `raw_finish_reason` 并存

```python
finish_reason: str | None = None
raw_finish_reason: str | None = None
```

`normalize_finish_reason()` 把 provider 的方言映射到六个标准值：

| 标准值 | 接受的原始值 |
|---|---|
| `stop` | `stop`、`end_turn`、`stop_sequence` |
| `length` | `length`、`max_tokens` |
| `tool_calls` | `tool_calls`、`function_call`、`tool_use` |
| `content_filter` | `content_filter`、`content-filter`、`refusal` |
| `error` | `error` |
| `other` | 其它任何值 |

**保留原始值**是关键：归一化成 `other` 之后，诊断时还能看到 provider 到底说了什么。

### 4.2 `*_declared` 布尔值

```python
@dataclass(frozen=True)
class ModelLimits:
    context_window: int
    context_window_declared: bool
    max_output_tokens: int
    max_output_tokens_declared: bool
```

保守回退值：

```python
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
```

**区分"模型声明了 128k"和"没声明所以我猜 128k"**。上下文预算计算需要知道自己在精确还是在猜——一个猜出来的窗口应该配更保守的安全余量。

### 4.3 `ProviderCapabilities`

```python
streaming: bool = True
tool_calls: bool = True
prefix_cache_key: bool = False
```

`prefix_cache_key` 默认 `False` 很关键。ReAct 循环里：

```python
provider_prefix_cache_key = (
    fit.prefix_cache_key
    if bool(getattr(getattr(llm, "capabilities", None), "prefix_cache_key", False))
    else None
)
```

**只有声明支持的 adapter 才会收到缓存键**。给不支持的 provider 传一个未知参数，轻则被忽略，重则 400。

### 4.4 `thinking` 默认关

```python
# Ask the model to think before answering.  Off by default: the request
# shape is rejected outright by models that do not support it, and the
# engine cannot tell from a relay's model name whether this one does.
# Each route builds its own client, so this is already per-route.
thinking: bool = False
```

**引擎无法从中转站给的模型名判断这个模型支不支持推理**——中转站的模型名可以是任意字符串。所以默认关，由用户显式开。

### 4.5 三级异常

```python
LLMError                    # 基类
└── LLMResponseError        # payload 不符合内部契约
    └── LLMContextLengthError   # 上下文超限（带 http_status + provider_code）
UnsupportedProviderError    # 配置里的 adapter 名没注册
```

`LLMContextLengthError` 被单独提出来，是因为 ReAct 循环要**专门处理**它——`_is_context_limit_error()` 命中后会触发一次上下文恢复重试（见 [04 · Engine 核心执行](./04-Engine-核心执行.md) §4.1），而不是直接失败。

而且它是"typed, **sanitized** rejection"——错误消息经过清洗，不把 provider 返回的原始 payload 抛给上层。

---

## 5. 流式事件

`engine/llm/events.py` 定义六种归一化的 provider 事件：

```mermaid
sequenceDiagram
    participant P as Provider
    participant A as Adapter
    participant C as ProviderClient
    participant R as ReAct 循环

    P->>A: 原生 chunk
    A->>C: RESPONSE_CREATED
    loop 流式
        A->>C: OUTPUT_TEXT_DELTA
        A->>C: REASONING_DELTA
        A->>C: FUNCTION_CALL_ARGUMENTS_DELTA
    end
    A->>C: USAGE
    A->>C: RESPONSE_COMPLETED
    C->>R: ProviderEvent（引擎内部契约）
    R->>R: _ProviderResponseAccumulator.build() → ChatResponse
```

模块 docstring 划清了边界：

> 这些名字刻意镜像了 OpenAI Responses 流式词汇里有用的那部分。它们是**引擎内部契约，不是 HTTP/SSE 线格式**：provider 适配器把它们的原生 chunk 翻译到这里，执行层决定哪些事件变成用户可见的进度。

`ProviderEvent.data` 也有一条约束：

> `data` 刻意被限制在引擎需要的信息上；**原始 provider payload 留在 provider 适配器内部，不会被意外暴露给前端**。

这一条是安全考量：provider 的原始响应里可能有请求 id、内部字段、甚至回显的 prompt 片段。

### 5.1 三种 delta 的意义差别

在 ReAct 循环里，这三种事件都算 `saw_content_event`：

| 事件 | 用户可见 | 为什么算"已开始生成" |
|---|---|---|
| `OUTPUT_TEXT_DELTA` | ✅ | 显然 |
| `REASONING_DELTA` | ❌ | **不可见，但证明 provider 已经开始响应**——重放会产生不同的工具计划 |
| `FUNCTION_CALL_ARGUMENTS_DELTA` | ❌ | 工具调用参数已经开始成形 |

---

## 6. 重试策略

`engine/llm/adapters/_retry.py` 只有 45 行：

```python
MAX_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 60.0

def is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500
```

```mermaid
flowchart TD
    A["HTTP 响应"] --> B{"429 或 5xx"}
    B -->|"否"| E["不重试"]
    B -->|"是"| C{"有 Retry-After 头"}
    C -->|"有，秒数"| D1["用它，但封顶 60 秒"]
    C -->|"有，HTTP 日期"| D2["解析成 delta，封顶 60 秒"]
    C -->|"无"| D3["指数退避 2^attempt"]
    D1 --> W["asyncio.sleep"]
    D2 --> W
    D3 --> W
    W --> R{"attempt < 3"}
    R -->|"是"| A
    R -->|"否"| F["放弃"]
```

`retry_after_seconds()` 的三条防御：

1. **接受两种格式**：纯秒数，或 HTTP 日期（`parsedate_to_datetime`）
2. **无时区的日期按 UTC 处理**（`retry_at.replace(tzinfo=timezone.utc)`）
3. **封顶 60 秒**，并拒绝非有限值和负值

第 3 条防的是：一个错误的 `Retry-After: 86400` 会让请求挂一天。

### 6.1 流式中断的重试

`586f92f fix(llm): retry a mid-stream provider overload on the OpenAI path` 处理的是一个 SDK 通常不管的场景：**流已经建立，中途 provider 返回过载**。

重试它的前提在 ReAct 循环那一侧（§5.1）：只有**还没出现任何语义 delta**才能重放。

---

## 7. `HTTPAdapterMixin`：六道流式边界

`engine/llm/adapters/_http.py`（239 行）的 docstring：

> 为 provider 适配器提供共享的 HTTP 管道：重试/退避循环、非流式 JSON 请求周期、**有界的响应读取**、以及每个基于 HTTP 的适配器都需要的错误体提取。适配器在实现 `ProviderAdapter` 协议的同时继承 `HTTPAdapterMixin`。

**六个上限**：

| 常量 | 值 | 防什么 |
|---|---|---|
| `MAX_RESPONSE_BYTES` | 20 MiB | 非流式响应过大 |
| `MAX_STREAM_TOTAL_BYTES` | 20 MiB | 整个流的总量 |
| `MAX_STREAM_EVENT_BYTES` | 1 MiB | 单个 SSE 事件过大 |
| `MAX_STREAM_EVENTS` | 10 000 | 事件条数 |
| `MAX_STREAM_DURATION_SECONDS` | 900（15 分钟） | 流的墙钟 |
| `MAX_ERROR_BODY_BYTES` | 64 KiB | 错误体读取上限 |

```mermaid
flowchart TD
    S["一次流式响应"] --> L["SSEStreamLimiter"]
    L --> C1{"单事件 > 1 MiB"}
    L --> C2{"总字节 > 20 MiB"}
    L --> C3{"事件数 > 10000"}
    L --> C4{"耗时 > 900 秒"}
    C1 -->|"是"| E["中止"]
    C2 -->|"是"| E
    C3 -->|"是"| E
    C4 -->|"是"| E

    style E fill:#ffcdd2
```

这套上限和 MCP 的四道边界（见 [12 · MCP 集成](./12-MCP-集成.md) §4）**是同一个设计模式的两次应用**：一个外部服务的响应流必须同时被字节、条数和时间三个维度约束，因为任何单一维度都有绕过方式。

**`MAX_ERROR_BODY_BYTES = 64 KiB`** 单独存在，是因为错误体也要读——一个返回 500 并附带 100 MB HTML 错误页的中转站，不该在读错误信息时把内存吃光。

### 7.1 上下文超限的文本识别

```python
_CONTEXT_LIMIT_MARKERS = (...)
```

provider 报告"上下文超了"的方式五花八门：HTTP 状态码可能是 400 也可能是 413，错误码字段可能叫 `code` / `type` / `error.code`，而有些中转站只在消息文本里说。

所以要**按文本标记识别**，然后抛 `LLMContextLengthError`——ReAct 循环的 `_is_context_limit_error()` 靠这个类型决定要不要触发一次上下文恢复重试（见 [04 · Engine 核心执行](./04-Engine-核心执行.md) §4.1）。

**把一个模糊的外部信号收敛成一个精确的内部类型**，是适配器层的核心价值。

---

## 8. Anthropic 适配器的四个翻译难点

`adapters/anthropic.py` 654 行，比 OpenAI 的 482 行大 35%。差额几乎全在**消息格式翻译**上——引擎内部用 OpenAI 风格的消息数组，Anthropic 的 Messages API 结构不同。

```mermaid
flowchart TD
    A["引擎的 OpenAI 风格消息"] --> B["_translate_messages()"]
    B --> C["_assistant_content()<br/>助手消息可能是 str 或 block 数组"]
    B --> D["_order_user_blocks()<br/>user 块必须按特定顺序"]
    B --> E["_merge_content() / _content_blocks()<br/>相邻同角色消息要合并"]
    B --> F["_append_message()"]
    A --> G["_translate_tools()<br/>function schema 转 tool schema"]
    B --> H["_with_cache_breakpoint()<br/>插入缓存断点"]
    H --> I["_request_body()"]
```

| 难点 | 处理 |
|---|---|
| **system 消息位置不同** | OpenAI 放在消息数组里，Anthropic 是顶层 `system` 字段 |
| **相邻同角色消息** | Anthropic 要求 user/assistant 交替，相邻同角色必须合并（`_merge_content`） |
| **内容可以是字符串或块数组** | `_content_blocks()` / `_copy_content()` / `_text_content()` 三个函数处理这个多态 |
| **user 块顺序有要求** | `_order_user_blocks()` 保证 tool_result 块排在文本块前面 |

### 8.1 缓存断点

```python
def _with_cache_breakpoint(...)
```

Anthropic 的 prompt 缓存需要在消息里插一个显式的 `cache_control` 标记。**断点位置就是 `prefix_cache_key` 对应的那个稳定前缀边界**（见 [04 · Engine 核心执行](./04-Engine-核心执行.md) §2.4）。

这解释了为什么 `ProviderCapabilities.prefix_cache_key` 默认是 `False`：OpenAI 的缓存是自动的（按前缀匹配），Anthropic 需要显式断点。**同一个概念在两个 provider 上的实现完全不同**，所以能力必须显式声明。

### 8.2 流式错误的可重试子集

```python
_RETRYABLE_STREAM_ERROR_TYPES = frozenset({...})

class _AnthropicStreamError(LLMResponseError):
    def __init__(self, message: str, *, retryable: bool) -> None: ...

class _AnthropicStreamTruncatedError(LLMResponseError): ...
```

Anthropic 的 SSE 流里可以携带 `error` 事件。**只有一部分错误类型值得重试**（比如 `overloaded_error`），其余（比如 `invalid_request_error`）重试只会得到同样的结果。

`_AnthropicStreamTruncatedError` 单独一个类型：流在没有收到结束事件的情况下断了。这和"收到了一个错误事件"是两回事——前者可能是网络问题，后者是 provider 明确的拒绝。

### 8.3 `_ANTHROPIC_VERSION = "2023-06-01"`

API 版本头。Anthropic 用日期做版本，写死一个已知可用的值比跟随最新更稳——**新版本可能改变响应结构**。

---

## 9. `ProviderClient`：适配器与 `LLMPort` 之间

`engine/llm/client.py`（261 行）做四件事，每件都有一处非显然的细节。

### 9.1 能力校验

```python
def _validate_requested_capabilities(self, ...) -> ...
```

调用方传了 `prefix_cache_key` 但适配器不支持时，**在这里被拦下**而不是传给适配器。

这是 [04 · Engine 核心执行](./04-Engine-核心执行.md) §4.1 里那段 `getattr(getattr(llm, "capabilities", None), "prefix_cache_key", False)` 的另一半——调用方先问，客户端再验，**两层都不信任对方**。

### 9.2 非流式伪装成事件流

```python
async def _complete_as_events(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
```

`stream=False` 时 `chat_events()` 仍然要产出事件——它调 `complete()` 拿完整响应，再**合成**一串事件（`RESPONSE_CREATED` → 一个大的 `OUTPUT_TEXT_DELTA` → `USAGE` → `RESPONSE_COMPLETED`）。

**调用方不需要写两套代码。** ReAct 循环里的流式/非流式分支处理的是"provider 能不能流"，而不是"事件流存不存在"。

### 9.3 首 token 延迟怎么测

```python
_CONTENT_EVENT_TYPES = (...)

async def _observed_stream(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
```

`ttft_ms` 的定义是"第一个**内容**事件的时间"，不是"第一个事件的时间"。`RESPONSE_CREATED` 立刻就到，用它测 TTFT 会得到一个恒定的小数字。

所以有 `_CONTENT_EVENT_TYPES` 这个集合——只有文本 delta、推理 delta、函数参数 delta 三种算内容。

### 9.4 记账在 finally 里

```python
async def _emit_generation(self, ...) -> None: ...
```

无论流正常结束、抛异常、还是被取消，**都要产出一条 `GenerationRecord`**（`ok` 字段区分）。一个只在成功路径记账的系统，会让"失败的调用"在成本报表里完全消失——而失败的调用**同样花钱**（provider 通常按输入 token 计费）。

---

## 10. 用量记账

`engine/llm/usage.py` 的 docstring 描述了一个现实问题：

> Provider 和网关用不同的方言报告 token 用量；**单个响应甚至可能混用方言**（一个 OpenAI 兼容网关代理 DeepSeek 时会同时返回 `prompt_tokens_details.cached_tokens` 和 `prompt_cache_hit_tokens`）。因此归一化是**按优先级匹配已知字段名**，而不是按 provider 分支。

六个归一化键：

```python
USAGE_KEYS = (
    "input_tokens", "output_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)
```

加一个不是 token 计数的标志位：

```python
# Not a token count — 1 when the provider sent a usage payload, 0 when it did not.
# Kept out of USAGE_KEYS so anything iterating token keys stays unaffected.
USAGE_REPORTED_KEY = "usage_reported"
```

**区分"用了 0 个 token"和"provider 没报"**——这两件事在成本统计里意义完全不同。而且它被刻意排除在 `USAGE_KEYS` 之外，所以任何遍历 token 键的代码都不受影响。

`_number()` 的三条拒绝：

```python
if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None      # bool 是 int 的子类，必须先排除
if value < 0:
    return None      # 负数不是合法用量
```

docstring 最后一句是原则：

> 细节字段**严格尽力而为**：缺失就是零，**绝不估算**。

---

## 11. Generation 级可观测

`engine/llm/observability.py`：**每一次模型调用都产生一条 `GenerationRecord`**——主循环和旁路一视同仁。

```python
@dataclass(frozen=True)
class GenerationRecord:
    provider: str
    model: str
    purpose: str          # interactive / gate / background / compaction / memory / routing / other
    usage: dict[str, int]
    ttft_ms: int | None   # 首 token 延迟
    total_ms: int
    stream: bool
    ok: bool
    run_id: str | None
    session_id: str | None
    occurred_at: str
    source_key: str       # uuid，落库时做幂等键
```

三个 contextvar 负责归因：

```mermaid
flowchart TD
    A["llm_purpose('gate')"] -->|"contextvar"| P["_purpose"]
    B["generation_context(run_id, session_id)"] -->|"contextvar"| S["_scope"]
    C["generation_sink(fn) 或 set_default_generation_sink(fn)"] --> K["_sink / _default_sink"]
    P --> R["GenerationRecord"]
    S --> R
    R --> K
    K --> DB[("llm_generations 表")]
```

**为什么用 contextvar 而不是参数传递**：一次记忆编译要经过 `maintenance → compile → _generate_view → ProviderClient`，把 `purpose='memory'` 一层层传下去会污染四个函数的签名。

docstring 里的一条硬约束：

> 记录严格是**尽力而为**：一个缺失或失败的 sink **绝不影响模型调用本身**。

`7b058ee fix(observability): attribute the two LLM side channels that logged NULL` 修的就是这个机制的两个漏网之鱼——两条旁路调用没设 `purpose`，落库时是 NULL，导致成本统计里出现归属不明的支出。

Server 侧在 `lifespan` 里装上默认 sink：

```python
set_default_generation_sink(TokenStatsService().record_generation)
```

关闭时置空：`set_default_generation_sink(None)`。

---

## 12. 录制与回放

`engine/llm/replay.py`（337 行）。`engine_runtime.py` 的 `_maybe_record()` 是入口：

```python
"""Set ``AGENT_SMITH_RECORD_LLM=/path/to/case.jsonl`` and every model turn of
every subsequent run appends there, ready to replay via engine.llm.replay.
Only the *responses* are written, never the prompt — so a recording cannot
leak conversation content, and replay does not need it (turns are served in
recorded order rather than matched against messages)."""
```

两条设计约束互相支撑：

```mermaid
flowchart LR
    A["只录响应，不录 prompt"] --> B["录制文件不含对话内容"]
    A --> C["回放不能按 prompt 匹配"]
    C --> D["改为按录制顺序供给"]
    D --> E["回放时 prompt 可以不同"]
    E --> F["同一份录制能测多种 prompt 变体"]

    style B fill:#e8f5e9
    style F fill:#e8f5e9
```

一个看起来是"隐私约束"的决定，副产品是**回放的适用范围更广**——因为不要求 prompt 一致，你可以改 prompt 再跑同一份录制，观察 harness 的行为变化。

`RecordingLLM` 和 `ReplayLLM` 都实现 `LLMPort`，所以它们对执行层完全透明。

### 12.1 `chat_events` 为什么不能走 `__getattr__`

`RecordingLLM` 用 `__getattr__` 把大部分属性透传给内层客户端：

```python
def __getattr__(self, name: str) -> Any:
    # stream / capabilities / context_window / model — whatever the loop reads.
    return getattr(self._inner, name)
```

但 `chat_events` **必须显式处理**，类 docstring 把三种写法的优劣排了序：

```python
if hasattr(inner, "chat_events"):
    self.chat_events = self._recording_chat_events
```

> `chat_events` is exposed **only when the wrapped client has it**, because `react_loop` probes streaming with `getattr(llm, "chat_events", None)`. Exposing a method that would fail is worse than not exposing one; and **delegating it through `__getattr__` is worse still** — that silently hands the loop the inner client's streaming method and **bypasses recording entirely**, which is exactly the bug this shape prevents.

三种写法的后果：

| 写法 | 后果 |
|---|---|
| 无条件定义 `chat_events` | 内层不支持流式时，循环探测到"支持"，调用即失败 |
| **交给 `__getattr__` 透传** | **循环拿到内层的方法，录制被完全绕过——而且没有任何报错** |
| 有条件地赋值实例属性 | 内层支持就录，不支持就不暴露 ✓ |

中间那种最危险：一切看起来都正常工作，只是录制文件里少了所有流式轮次。等到回放时才发现录制不完整，而那时已经无法复现当时的场景了。

`__getattr__` 只在**实例属性查找失败**时才调用，所以 `self.chat_events = ...` 这个实例属性会优先命中，透传不会发生。这是有意利用 Python 的属性查找顺序。

### 12.2 追加为什么要原子写加锁

```python
with self._lock:
    tmp = self._path.with_name(f"{self._path.name}.tmp")
    existing = self._path.read_text(...) if self._path.exists() else ""
    tmp.write_text(existing + line, encoding="utf-8")
    os.replace(tmp, self._path)
```

看起来很重——每次追加都要重写整个文件。两条注释分别解释了为什么：

**① 原子 rename 防截断。**

> Append through a same-directory temp file and an atomic rename so a crash mid-write **cannot leave a truncated final line** that would make the whole recording unloadable.

直接 `open(path,"a")` 追加，崩在写到一半时会留下半行 JSON。而 `load_recording` 读到那行会解析失败——早期版本会因此让整个录制文件不可用。

**② 锁防止并发丢行。**

> The lock serializes the read-modify-write: **two overlapping turns** (e.g. a compaction summary while the main loop records) must not both read the same tail and silently drop one line.

这是"读-改-写"的经典竞态：两个轮次同时读到同样的旧内容，各自加上自己的一行再写回，**后写的覆盖先写的**，丢一行。触发场景很具体——主循环正在录制时，上下文压缩也发起了一次 LLM 调用（见 [09 · Server API 层](./09-Server-API层.md) §4.6）。

即便如此，`load_recording` 仍然保留了跳过畸形行的能力：

```python
"""A crash during append can leave a truncated final line.  Malformed or
non-object lines are skipped (with a warning) so the rest of the recording
stays loadable instead of failing wholesale."""
```

注释说明这是"second line of defense for files written by older versions"——**为旧版本写的文件兜底**。新版本不会再产生截断行，但已经存在的录制文件不该因此报废。

### 12.3 流式录制要等流结束才写

```python
"""The write happens once, after the stream ends. Appending per event would
leave a half turn on the file when a consumer breaks early, and replay
would then serve that half turn as if it were complete."""
```

逐事件追加看起来更自然（边收边存，崩了也保留了一部分），但会产生一个更坏的结果：**消费者提前 `break`**（比如 ReAct 循环检测到工具调用就停止读流）时，文件里留下一个不完整的轮次。

回放时没有任何标记能说明"这个轮次是半截的"，`ReplayLLM` 会把它当成完整的一轮供给——测试于是在一个现实中从未发生过的输入上运行。**不完整的录制比没有录制更危险**，因为它看起来是有效的。

### 12.4 回放的两种失败

```python
class ReplayExhaustedError(RuntimeError):
    """The harness asked for more model turns than the recording holds."""

class ReplayShapeError(RuntimeError):
    """The harness asked for a turn in the shape the recording does not hold."""
```

两个异常对应两类回归，错误消息都写得很具体：

| 异常 | 触发 | 消息 |
|---|---|---|
| `ReplayExhaustedError` | 循环要的轮次比录制多 | `harness requested model turn 5 but the recording holds 4 — the loop now takes more turns than when this case was recorded` |
| `ReplayShapeError` | 形状不匹配（流式 ↔ 非流式） | `turn 2 was recorded as a stream — replay it through chat_events, not chat` |

第一个的消息末尾那句 "**the loop now takes more turns than when this case was recorded**" 直接指出了这是一次行为变化——不是回放坏了，是被测代码的轮次数变了。这正是回归测试想捕获的信号。

形状检查里有一个小细节被注释特意点出：

```python
# ``_next_turn`` has already advanced ``_index``, so the turn that
# just failed the shape check is ``_index - 1``, not ``_index``.
raise ReplayShapeError(f"turn {self._index - 1} was recorded as a stream — ...")
```

`_next_turn()` 取完就自增了游标，所以报错时要用 `_index - 1` 才指向真正出问题的那一轮。差一错误出现在错误消息里尤其恶劣——它会把排查引向错误的那一行录制。

### 12.5 `ReplayLLM` 声明的保守默认值

```python
# A recording does not capture the original route's capacity facts, so
# replay reports the same conservative defaults the engine falls back
# to for any client that omits them.  Declaring them explicitly keeps
# ReplayLLM a complete LLMPort instead of relying on getattr fallbacks.
self.capabilities = ProviderCapabilities(streaming=self.stream, tool_calls=True, prefix_cache_key=False)
self.context_window = DEFAULT_CONTEXT_WINDOW
self.context_window_declared = False
self.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
self.max_output_tokens_declared = False
```

录制文件里没有上下文窗口、最大输出这些**容量事实**——它们属于当时那条路由的配置，不属于响应。回放时只能用引擎的保守默认值。

关键是 `*_declared = False`（见 §4.2）：明确标记"这些数字不是 provider 声明的，只是兜底值"。依赖它们做精确预算的代码因此知道该留余量。

`prefix_cache_key=False` 也是必然的——回放不会真的调用模型，缓存提示毫无意义。`_replay_chat_events` 里对这个参数的处理很直白：

```python
# ``prefix_cache_key`` is accepted for LLMPort signature parity but
# deliberately ignored: recorded turns are served in order, they are
# never regenerated, so no cache hint is meaningful here.
```

**接受参数但显式忽略**，并在注释里说明为什么——比悄悄丢弃或者干脆不接受这个参数都好。不接受会破坏 `LLMPort` 的签名一致性，悄悄丢弃则会让下一个读代码的人怀疑是不是漏实现了。

`stream` 和 `chat_events` 的存在与否**跟着录制走**：

```python
self.stream = bool(self._turns) and self._turns[0].is_streaming
if self.stream:
    self.chat_events = self._replay_chat_events
```

这样回放时 ReAct 循环会走**和录制时完全相同的路径**——录的是流式就走流式分支，录的是非流式就走非流式分支。如果回放总是暴露 `chat_events`，一份非流式录制会让循环走进流式分支，测的就不是当初那条路了。

---

## 13. 客户端缓存

`engine/llm/client.py`（261 行）是 adapter 和 `LLMPort` 之间的那一层。它做四件事：

| 职责 | 说明 |
|---|---|
| 协议适配 | `ProviderAdapter.complete/stream_response` 转成 `LLMPort.chat/chat_events/chat_stream` |
| 流式开关 | `stream=False` 时 `chat_events` 走非流式路径 |
| 用量归一化 | 调 `usage.py` |
| 生成记录 | 每次调用产出一条 `GenerationRecord` |

`chat_stream()` 是一个只产出文本的简化接口，给不需要完整事件的调用方用。

### 13.1 指纹

`server/app/services/engine_runtime.py` 的 `LLMClientManager`：

```python
def get_for_config(self, config) -> LLMPort:
    fingerprint = _config_fingerprint(config)
    with self._lock:
        client = self._clients.get(fingerprint)
        if client is None:
            client = _maybe_record(build_llm_client(config))
            self._clients[fingerprint] = client
        return client
```

**按解析后的完整配置做指纹**，所以三条路由如果配置相同就共享一个客户端。关闭时去重：

```python
clients = list({id(client): client for client in self._clients.values()}.values())
```

因为多个指纹可能映射到同一个对象（不会，但防御性写法），且 `RuntimeServices.close()` 那边也有同样的去重。

---

## 14. 接一个新 Provider 要做什么

```mermaid
flowchart TD
    A["1. 写 adapters/xxx.py<br/>实现 ProviderAdapter 协议"] --> B["2. complete() 返回 ChatResponse"]
    B --> C["3. stream_response() 产出 ProviderEvent"]
    C --> D["4. 声明 ProviderCapabilities"]
    D --> E["5. 声明 context_window / max_output_tokens<br/>以及它们的 *_declared"]
    E --> F["6. factory.py 里 register()"]
    F --> G["7. 复用 _http.py 和 _retry.py"]
    G --> H["完成——执行层零改动"]

    style H fill:#e8f5e9
```

四个协议成员是全部契约：

```python
provider: str
capabilities: ProviderCapabilities
context_window / context_window_declared
max_output_tokens / max_output_tokens_declared

async def complete(self, request: LLMRequest) -> ChatResponse
def stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]
async def close(self) -> None
```

**执行层不需要改一行。** 这是把 `LLMPort` 和 `ProviderAdapter` 分成两个 Protocol 的收益。

---

## 15. 中转站的现实问题

用中转站（relay）而不是官方端点时，有三条实测教训：

**① `/v1/models` 列出的模型不等于能用。**
中转站的模型列表往往是从上游抄的，实际没开通道。**换模型前先 `curl` 直接打一次**。

**② 有的模型只走 Responses API，不走 Chat Completions。**
同一个中转站的不同模型可能需要不同的端点形状。

**③ 模型名是任意字符串。**
这就是 `thinking` 默认关的原因——引擎无法从名字推断能力。

对应的设计选择：

| 现实问题 | 设计应对 |
|---|---|
| 模型能力不可推断 | `thinking` / `prefix_cache_key` 默认关，显式开启 |
| 用量方言混乱 | 按字段名优先级匹配，不按 provider 分支 |
| 上游过载 | 429/5xx 重试，尊重 `Retry-After` 但封顶 60 秒 |
| 窗口未声明 | `*_declared` 布尔值让下游知道自己在猜 |
| 协议方言 | `openai_compatible` 别名 + `normalize_finish_reason` |

### 15.1 `openai.py` 里六处为中转站做的妥协

`engine/llm/adapters/openai.py`（482 行）比协议本身要求的复杂得多，多出来的部分几乎全在处理中转站的现实。六处注释各记录了一个具体问题。

**① 同一个字段有两种拼写。**

```python
# Both spellings occur in the wild; the streaming path already accepts
# either, and reading only reasoning_content here silently dropped the
# whole reasoning block for relays that use `reasoning`.
```

推理内容有的中转站叫 `reasoning_content`，有的叫 `reasoning`。非流式路径原本只读前者——遇到用后者的中转站，**整个推理块被静默丢弃**。用户看到的是"这个模型的思考过程没显示出来"，而不是任何错误。

**② 非字符串的值要丢弃，不能抛错。**

```python
# A non-string value is discarded rather than raised on: some relays
# return structured reasoning, and the streaming path (see
# _stream_response) already drops those silently.  Raising here would
# make the same relay work when streaming and fail when not.
```

有的中转站返回结构化的 reasoning（对象而非字符串）。这里如果抛异常，会造成一个非常难排查的现象：**同一个中转站，流式能用，非流式报错**。

注释点明了判断依据——不是"哪种处理更严谨"，而是"两条路径必须一致"。宽松和严格都可以，但两边不一样一定是错的。

**③ 流中的 `error` 对象必须读，否则真实原因被掩盖。**

```python
# Relays commonly report a mid-stream failure as an `error`
# object and then drop the connection.  Reading only
# `choices` discarded that and left the caller with
# "stream ended before the [DONE] sentinel", hiding the real
# cause (rate limit, content filter, bad credentials).
```

标准的 SSE 流里每个数据块都带 `choices`。但中转站在流中途失败时，常常发一个 `{"error": {...}}` 然后直接断连——**没有 `[DONE]` 哨兵**。

只解析 `choices` 的实现会忽略这个错误块，然后在流结束时报一句 "stream ended before the [DONE] sentinel"。这个消息**完全掩盖了真实原因**：可能是限流、可能是内容过滤、可能是凭据失效，三种问题的处理方式完全不同，而用户只看到一句"流提前结束了"。

**④ 但错误文本绝不能带进异常。**

```python
# The provider's error text is deliberately NOT carried into
# the exception: relays echo request content (including the
# prompt) into it, and provider error bodies must not
# surface in exceptions or logs (see the boundary enforced
# for HTTP bodies in ``HTTPAdapterMixin._raise_for_status``).
```

③ 和 ④ 看起来矛盾——要读错误对象，但不能用它的文本。解释是：**读它是为了知道错误的类型，不是为了转述它的内容**。

中转站会把请求内容（**包括完整的 prompt**）回显进错误消息。如果把这段文本放进异常，它会流进日志、流进 trace、流进事故记录——等于把对话内容泄漏到所有观测通道里。这条边界和 `HTTPAdapterMixin._raise_for_status` 对 HTTP 响应体的处理是同一条（见 §7）。

所以适配器只从错误对象里取**结构化的分类字段**（`type` / `code` / `status`），扔掉 `message`。

**⑤ 分类字段要归一化后再匹配。**

```python
# Enum-like ``type``/``code`` values that mark a mid-stream failure as
# transient.  Both sides are stripped to letters and digits before matching,
# because relays spell the same condition as "rate_limit_exceeded",
# "rate-limit-error", or "RateLimitReached".  The free-text ``message`` is
# never consulted: relays echo request content (including the prompt) into it.
_NON_ALNUM = re.compile(r"[^a-z0-9]")
```

同一个"限流"，三家中转站三种写法：

| 写法 | 归一化后 |
|---|---|
| `rate_limit_exceeded` | `ratelimitexceeded` |
| `rate-limit-error` | `ratelimiterror` |
| `RateLimitReached` | `ratelimitreached` |

**两边都剥离成小写字母和数字**再做子串匹配，三种写法都能命中 `ratelimit`。这比维护一张穷举表可靠——新的中转站会发明新的拼写，但词根不会变。

注释里再次强调 "The free-text `message` is **never** consulted"——同一条约束在两处注释里重复出现，说明它容易被后来的改动破坏。

**⑥ 200 响应里的错误也要按瞬时失败重试。**

```python
_RETRYABLE_STREAM_ERROR_STATUS = frozenset({"429", "500", "502", "503", "504"})
```

```python
# A 200 stream carrying an overload/rate-limit error member is
# the same transient failure the HTTP path already retries; only ...
```

HTTP 状态码是 200（流成功建立了），但流内容里携带 429 语义的错误——这在中转站上很常见，因为它们先接受连接再去问上游。

对调用方而言这和直接返回 429 没有区别，都是**上游暂时过载**，都应该重试。适配器因此把流内错误映射到同一套重试逻辑上。`_StreamProviderError` 带一个 `retryable` 布尔，`_StreamTruncatedError` 则表示流被截断——两个都继承 `LLMResponseError`，让上层用统一的方式处理。

### 15.2 这些妥协的共同形状

六处妥协可以归成三类：

```mermaid
flowchart TD
    subgraph A["① 接受方言"]
        A1["两种字段拼写都读"]
        A2["分类值归一化后匹配"]
        A3["200 里的错误也算错误"]
    end
    subgraph B["② 保持路径一致"]
        B1["流式与非流式对同一输入<br/>必须同样成功或同样失败"]
    end
    subgraph C["③ 守住泄漏边界"]
        C1["只取结构化分类字段"]
        C2["自由文本一律不用"]
    end

    style C fill:#ffcdd2
```

第③类最容易在"改进错误提示"时被破坏——把 provider 的错误消息透出来看起来对用户更友好，实际上会把 prompt 泄漏进日志。这也是为什么两处注释都写了同一句警告。

对接一个新的 OpenAI 兼容端点时，这六处就是最可能出问题的地方。§14 的清单可以配合这一节一起看。

---

## 16. 参数速查

| 参数 | 值 | 位置 |
|---|---|---|
| 默认上下文窗口（未声明时） | 128 000 | `contracts.py` |
| 默认最大输出（未声明时） | 4 096 | `contracts.py` |
| 最大重试次数 | 3 | `_retry.py` |
| `Retry-After` 上限 | 60 秒 | `_retry.py` |
| 可重试状态码 | 429、>= 500 | `_retry.py` |
| 退避策略 | `2^attempt` 秒 | `_retry.py` |
| interactive 超时 | connect 10 / read 90 / stream 120 / write 30 / pool 10 | `model_config.py` |
| gate 超时 | connect 10 / read 90 / stream 90 / write 30 / pool 10 | 同上 |
| background 超时 | connect 10 / read 240 / stream 300 / write 30 / pool 10 | 同上 |
| 支持的 provider | `openai`（别名 `openai_compatible`）、`anthropic` | `factory.py` |
| `stream` 默认 | `true` | `config_fields.py` |
| `thinking` 默认 | `false` | `config_fields.py` |
| 归一化用量键 | 6 个 + `usage_reported` | `usage.py` |
| 归一化 finish_reason | 6 个标准值 | `events.py` |
| provider 事件类型 | 6 种 | `events.py` |

---

## 17. 设计取舍

**① 两个 Protocol 而不是一个。** `LLMPort` 给执行层，`ProviderAdapter` 给 provider 实现。多一层，但换来"加录制/回放/测试替身"和"加 provider"是两件互不干扰的事。

**② 手写适配器而不是用官方 SDK。** 换来超时分档、流式中断重试、中转站兼容、录制回放四件事的完全控制。代价是 1.1k 行适配器代码要自己维护。

**③ 归一化时保留原始值。** `raw_finish_reason` 与 `finish_reason` 并存。归一化让代码简单，原始值让诊断可能。

**④ 能力默认关。** `thinking`、`prefix_cache_key` 都默认 `false`。因为引擎无法探测能力，而错误开启的代价是请求直接被拒。

**⑤ 记账严格但尽力而为。** 用量缺失就是零、绝不估算；sink 失败绝不影响模型调用。**观测不能反过来伤害被观测的东西。**

**⑥ 用 contextvar 做归因。** 避免把 `purpose` 沿四层调用链传递。代价是归因错误比较难查——`7b058ee` 修的就是两条忘了设 purpose 的旁路。

---

### 17.1 这一层反复出现的六条手法

3 200 行分在 17 个文件里，但同样的处理方式在两个适配器、客户端、录制回放之间反复出现。

**① 结构化字段可信，自由文本不可信。** 重试分类只看 `type`/`code`/`status`（§15.1 ⑤），错误消息一律不进异常（§15.1 ④）、不参与判断。理由有两个：文本会变（provider 改文案就失效），文本会带 prompt（泄漏）。这条在 [10 · 可观测性与诊断](./10-可观测性与诊断.md) §5.2.3 区分工具超时与审批超时时也成立。

**② 方言要归一化，不要穷举。** `_NON_ALNUM` 剥离非字母数字后匹配词根，而不是维护一张 `rate_limit_exceeded` / `rate-limit-error` / `RateLimitReached` 的对照表。穷举表面对新中转站一定会漏，词根不会。

**③ 两条路径对同一输入的行为必须一致。** 流式与非流式对结构化 reasoning 的处理（§15.1 ②）、流内错误与 HTTP 错误的重试分类（§15.1 ⑥）、录制与回放走同一条分支（§12.5）。**分歧比宽松或严格都糟**——它制造的是"时灵时不灵"。

**④ 不确定的数字要标记出来。** `context_window_declared` / `max_output_tokens_declared`（§4.2）、用量缺字段填零而非估算（§18.3）、回放的保守默认值（§12.5）。让下游知道"这个数是猜的"，比给一个看起来精确的假数字有价值。

**⑤ 副作用一旦对外可见就不能回滚。** 内容 delta 发出后不再重试（§18.2），录制在流结束后一次性写（§12.3）。判断标准是"外部是否已经观察到"，不是"内部状态是否干净"。

**⑥ 上限要在代价发生之前生效。** 响应体大小在解析前检查、SSE 事件逐个计预算、`Retry-After` 封顶。检查放在代价之后就没有意义了——`json.loads()` 之后再判断"响应太大"，内存已经吃掉了。

### 17.2 接一个新 provider 之前先问三个问题

**① 它的错误分类字段叫什么？** 不是"错误消息长什么样"。找到它用来标记限流、过载、内容过滤的**枚举字段**，把词根加进 `_RETRYABLE_STREAM_ERROR_MARKERS`。如果它只有自由文本没有分类字段，那就只能靠 HTTP 状态码——但绝不要去 grep 消息文本。

**② 它的流式失败长什么样？** 三种可能：断连、发 error 对象、发一个带错误的正常事件。§15.1 ③ 说的就是第二种最容易被漏掉。三种都要能识别，且都要能区分"内容前"和"内容后"（§18.2）。

**③ 它声明容量事实吗？** 上下文窗口和最大输出如果 provider 不给，就用默认值并把 `*_declared` 设成 `False`。**不要去猜**——从模型名推断窗口大小是 §15 ③ 明确否决过的做法，因为模型名是任意字符串。

三个问题分别对应 §15.1、§18.2、§4.2。§14 那份清单是操作步骤，这三个问题是操作之前要先搞清楚的事实。

搞清楚之后，实现顺序建议是：先跑通非流式 `complete()`，用 `test_llm_end_to_end.py` 那六个测试的形状验证一遍；再实现 `stream_response()`，重点测内容前后的失败分类；最后才去处理用量字段和缓存提示这些锦上添花的部分。

理由是**错误路径比正常路径更难补**。正常响应的解析写错了，第一次调用就会暴露；而"内容开始后仍在重试"这类问题只在特定的失败时序下出现，可能上线几周才遇到一次，且现场难以复现。先把失败分类跑对，后面加功能都是在一个可靠的基础上。

对接完成后，用 `AGENT_SMITH_RECORD_LLM` 录一份真实交互（§12），它会成为这个 provider 的回归基线——比手写 mock 更接近真实，也不会因为你对 provider 行为的理解有偏差而写出一份错误的期望。

---

## 18. 测试锁住了什么

`engine/tests/llm/` **97 个测试**：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_llm_adapters.py` | 30 | 两个适配器的翻译与流式 |
| `test_llm_client.py` | 22 | `ProviderClient`、重试、超时 |
| `test_replay.py` | 14 | 录制与回放 |
| `test_llm_usage.py` | 8 | 用量方言与清洗 |
| `test_llm_client_generation.py` | 8 | generation 记账 |
| `test_llm_generation_observability.py` | 7 | 观测通道 |
| `test_llm_end_to_end.py` | 6 | 完整链路 |
| `test_price_table.py` | 2 | 价格表解析 |

### 18.1 三处泄漏边界，三个测试

§15.1 ④ 说 provider 的错误文本绝不能进异常。三个测试分别守住三条路径：

| 测试 | 路径 |
|---|---|
| `request_failure_does_not_surface_provider_error_body` | HTTP 错误响应体 |
| `openai_stream_error_does_not_surface_provider_message` | OpenAI 流内 error 对象 |
| `anthropic_stream_error_does_not_surface_provider_message` | Anthropic 流内 error 事件 |

三条路径、三个测试、同一条约束。加上 `api_key_hidden_from_config_repr`（配置对象的 `repr` 不能暴露 key），构成这一层的四道泄漏防线。

`build_llm_client_rejects_insecure_or_private_endpoints` 则守着 §2.2.1 的 SSRF 校验——它是唯一能在**凭据发出之前**拦住错误端点的地方。

### 18.2 重试的边界：内容一旦开始就不再重试

这是本层最重要的不变量，五个测试从不同角度钉它：

| 测试 | 场景 |
|---|---|
| `chat_events_does_not_retry_after_content_delta` | 已经吐出内容 → **不重试** |
| `anthropic_truncation_after_content_is_not_retried` | 内容后截断 → 不重试 |
| `openai_retries_a_pre_content_truncated_stream` | 内容**前**截断 → 可以重试 |
| `anthropic_retries_a_pre_content_overloaded_stream_error` | 内容前过载 → 可以重试 |
| **`chat_events_discards_pre_content_events_from_failed_attempt`** | 重试时**丢弃**上一次尝试已发出的事件 |

分界线是"有没有内容 delta 发给调用方"：

```mermaid
flowchart LR
    A["流开始"] --> B{"发出过<br/>content delta 吗"}
    B -->|"没有"| C["失败可重试<br/>丢弃已发事件"]
    B -->|"发过"| D["失败不可重试<br/>否则用户会看到<br/>重复或矛盾的文本"]

    style C fill:#e8f5e9
    style D fill:#ffcdd2
```

一旦内容进了用户的屏幕，重试就会产生重复输出——第二次尝试从头生成，用户看到同一段话说了两遍，或者两段互相矛盾的话。所以重试窗口只在**第一个内容 delta 之前**开着。

最后那个测试守的是另一半：重试**之前**必须把上一次尝试已经发出的非内容事件（started、role 之类）丢掉，否则调用方会收到两次 started。

`openai_stream_error_retry_classification_matches_the_http_path` 对应 §15.1 ⑥——流内错误和 HTTP 错误必须用**同一套**重试分类，两边分歧会导致同一个上游故障在流式和非流式下表现不同。

`provider_retry_after_is_bounded` 守着 `Retry-After` 的 60 秒封顶：尊重服务端的建议，但不允许它把一次请求挂起几小时。

### 18.3 用量方言的四个测试

| 测试 | 锁住 |
|---|---|
| `openai_style_fields_map_to_all_six_keys` | 标准字段全覆盖 |
| `deepseek_cache_hit_maps_to_cache_read_and_miss_is_ignored` | DeepSeek 的缓存字段翻译 |
| **`openai_detail_takes_priority_over_deepseek_alias`** | 两种方言同时出现时的**优先级** |
| `empty_usage_object_counts_as_unreported` | 空对象 ≠ 全零 |

第三个是关键：有的中转站会同时返回两套字段名，此时必须有确定的优先级，否则同一份响应两次解析可能得到不同结果。

清洗侧另有三个：`missing_fields_default_to_zero_without_estimation`（缺字段填零，**不估算**）、`negative_and_float_values_are_sanitized`（负数和浮点要清洗）、`invalid_payloads_normalize_to_all_zero`（畸形载荷归零）。

"不估算"这条尤其重要——一个估算出来的 token 数会混进成本统计，而使用者无法分辨哪些是真实上报、哪些是猜的。宁可是零，也不要一个看起来合理的假数字。这和 [10 · 可观测性与诊断](./10-可观测性与诊断.md) §8.5 里 `None` 与 `0.0` 的区分是同一种考虑。

### 18.4 录制与回放：14 个测试

§12 的每一条设计都有对应测试：

| 测试 | 对应 |
|---|---|
| **`recorder_does_not_invent_streaming_for_a_plain_client`** | §12.1 `chat_events` 不能透传 |
| `recorder_forwards_prefix_cache_key_to_inner_stream` | 参数要转发给内层 |
| **`recorder_appends_atomically_without_leaving_temp_file`** | §12.2 原子写且不留临时文件 |
| `load_recording_skips_a_truncated_final_line` | §12.2 截断行容错 |
| `load_recording_skips_unknown_event_types` | 未知事件类型跳过 |
| **`half_streamed_turn_is_not_recorded`** | §12.3 半截轮次不录 |
| `replay_refuses_to_invent_turns_the_recording_lacks` | §12.4 耗尽即报错 |
| `replay_shape_error_names_the_offending_turn_when_chat` | §12.4 的 `_index - 1` |
| `replay_shape_error_names_the_offending_turn_when_streaming` | 同上，另一个方向 |
| `replaying_a_stream_through_chat_is_refused` | 形状必须匹配 |
| `record_then_replay_round_trip` | 往返一致 |
| `streaming_turns_are_recorded_and_replayed_verbatim` | 流式逐字还原 |
| `recording_survives_a_tool_call_round_trip` | 含工具调用的轮次 |
| **`replay_catches_a_real_harness_behaviour_change`** | **回放机制本身有效** |

最后一个是元测试——它验证的不是某个函数，而是**整套回放机制真的能捕获行为变化**。没有它，前面十三个测试可能全绿而回放实际上抓不到任何回归。

`recording_then_replay_reproduces_the_same_run` 则是端到端确认：录一次、放一次，两次运行的结果完全相同。

### 18.5 其余值得一提的

| 测试 | 锁住 |
|---|---|
| `client_uses_distinct_non_stream_and_stream_timeouts` | 两种调用用不同超时档（§2.3） |
| `usage_only_sse_events_each_get_an_independent_byte_budget` | 每个 SSE 事件独立计字节预算（§7） |
| `openai_response_size_cap_aborts_before_full_parse` | **解析前**就中止超大响应 |
| `anthropic_cache_breakpoint_does_not_mutate_caller_history` | 缓存断点不能改调用方传入的消息列表 |
| `anthropic_moves_late_system_instruction_into_ordered_user_turn` | 迟到的 system 指令要转成有序的 user 轮次 |
| `emit_swallows_sink_failures` | 观测 sink 失败不影响主流程 |
| `stream_abandoned_after_completion_records_success` | 完成后才断流仍记成功 |
| `context_length_rejection_is_recovered_end_to_end` | 上下文超限能端到端恢复 |

`openai_response_size_cap_aborts_before_full_parse` 里的 "before full parse" 是要点：上限必须在**解析之前**生效。如果先 `json.loads()` 再检查大小，那个 500 MB 的响应体已经进内存了，检查为时已晚。

`anthropic_cache_breakpoint_does_not_mutate_caller_history` 守的是一条容易违反的约定：适配器为了加缓存断点需要改消息结构，但**不能改调用方传进来的那个列表**。就地修改会让 ReAct 循环的历史被污染，下一轮请求带上适配器的内部标记。

---

## 19. 接下来

| 想深入 | 读 |
|---|---|
| 配置怎么从 YAML 到请求 | [02 · 快速上手](./02-快速上手.md) §3–§4 |
| 事件怎么变成 ReAct 循环的行为 | [04 · Engine 核心执行](./04-Engine-核心执行.md) §4 |
| `GenerationRecord` 怎么落库 | [10 · 可观测性与诊断](./10-可观测性与诊断.md) |
| 门禁与记忆编译各走哪条路由 | [05 · 记忆系统](./05-记忆系统.md) §9.4 |
| `/api/config/llm` 端点 | [09 · Server API 层](./09-Server-API层.md) |
