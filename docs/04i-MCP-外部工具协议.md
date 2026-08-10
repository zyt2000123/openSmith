# 04i · MCP 外部工具协议

## 本章结论

`engine/mcp/` 让 Smith 作为 MCP client 接入 stdio 与 streamable HTTP 服务，并将远端工具转换为受本地 registry、安全、Run 生命周期管理的工具。它不是对外 MCP server，也不能绕过工具治理。

## 总体架构

```mermaid
graph LR
    C[MCP config] --> T[transport selection]
    T --> S[stdio / streamable HTTP]
    S --> M[MCPClient]
    M --> P[MCPClientSessionPool]
    P --> R[register configured tools]
    R --> G[ToolRegistry + Safety]
    G --> E[Run event / tool result]
```

## 目录与职责

| 文件 | 设计职责 | 核心对象 |
| --- | --- | --- |
| `client.py` | MCP transport、协议请求、工具列举与调用、名称去重 | `MCPClient`、`MCPTool`、`StdioMCPTransport`、`StreamableHTTPMCPTransport` |
| `config.py` | 读取配置、选择 transport、注册带前缀工具 | `register_configured_mcp_tools()`、`MCPRegistration` |
| `session_pool.py` | 以配置 fingerprint 复用/重建 session，并关闭过期资源 | `MCPClientSessionPool` |

## 调用链

```text
profile config → mcp_transport_from_config → MCPClient.connect/list_tools
listed MCPTool → register_mcp_tools_with_prefix → ToolRegistry
LLM tool call → ToolPolicy / ToolGuard / Approval → MCPClient.call_tool
connection failure → invalidate/reconnect session → bounded error event
```

## 核心设计

### 协议适配与工具治理分离

MCP client 负责 JSON-RPC/transport 生命周期，不决定是否允许某个远端工具做副作用。注册后的 MCP 工具与本地 provider 一样进入 ToolRegistry，再经过同一套 policy、guard、approval 与 ledger。这避免“远端服务”成为权限旁路。

### 会话池按配置而非进程永久复用

`MCPClientSessionPool` 以配置 fingerprint 管理 session；配置变化时旧连接会关闭并重建。这样避免将过期 URL、环境变量或子进程带到后续 Run，也让资源关闭有明确所有者。

### 名称冲突必须显式处理

注册工具使用 server prefix、安全名称规范化和去重规则；同名远端工具不能悄悄覆盖本地工具。调用错误、SSE 响应与大响应也都通过有界读取和错误类型归一化处理。

## 失败语义与边界

| 情况 | 结果 |
| --- | --- |
| 配置无效或 transport 不支持 | 不注册该服务，返回可诊断失败 |
| 连接已失效或 fatal error | session 失效并由后续受控重连恢复 |
| 工具重名 | 使用前缀/去重名称，不覆盖既有 registry 项 |
| 子进程或 URL 含 secret | 日志摘要脱敏，子进程环境按白名单构造 |
| MCP 工具请求副作用 | 仍走本地安全与审批链 |

## 自测题

1. 为什么 MCP 工具不能在 client 层直接执行？
2. 为什么 session pool 的 key 是配置 fingerprint？
