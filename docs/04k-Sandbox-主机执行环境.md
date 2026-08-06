# 04k · Sandbox 主机执行环境

## 本章结论

`engine/sandbox/` 将 Shell 类工具的“实际执行”与工具协议分离：`host.py` 管理异步子进程、输出上限、取消和进程组终止；`macos_seatbelt.py` 在 macOS 上构造额外的文件系统约束。它是安全体系的执行环境层，不替代 [04g · Safety](04g-Safety-安全与审批.md) 的策略和审批。

## 总体架构

```mermaid
graph LR
    C[Approved shell call] --> G[ToolGuard]
    G --> E[ExecutionEnvironment]
    E --> L[LocalExecutionEnvironment]
    L --> S[macOS Seatbelt optional]
    S --> P[process group]
    P --> O[bounded stdout / stderr]
    O --> R[CommandResult]
    R --> T[Tool result event]
```

## 目录与职责

| 文件 | 设计职责 | 核心对象 |
| --- | --- | --- |
| `host.py` | 创建子进程、限制 I/O、处理 timeout/cancel、终止整组进程 | `ExecutionEnvironment`、`LocalExecutionEnvironment`、`CommandResult` |
| `macos_seatbelt.py` | 在 macOS 生成受保护路径约束的执行环境 | `MacOSSeatbeltEnvironment` |

## 核心设计

### 执行过程必须可取消且不遗留子进程

命令可能启动子孙进程。`LocalExecutionEnvironment` 以进程组运行并在取消或超时时终止整个组，再 drain/cancel 读取任务；只终止父进程会留下后台写入或端口占用，因此不满足 Run 取消语义。

### 输出是受限资源

stdout/stderr 通过有限 buffer 读取并记录总量，`CommandResult` 给调用方可展示的文本而不是无限字节流。这样一个失控命令不会耗尽内存、污染 prompt，也不会阻塞取消清理。

### Seatbelt 是纵深防御

macOS Seatbelt 根据受保护数据路径和 hard-link 风险增加操作系统侧限制；它不能决定用户是否同意某次写入，也不能替代 ToolGuard 的路径/命令判定。跨平台环境没有 Seatbelt 时，核心安全语义仍由 guard 与审批保证。

## 调用链与失败语义

```text
ToolRegistry → ToolPolicy / ToolGuard / Approval → ExecutionEnvironment.execute
execute → spawn process group → bounded stream readers → CommandResult
timeout/cancel → signal process group → drain/cancel readers → failure result/event
```

| 情况 | 结果 |
| --- | --- |
| timeout 或取消 | 终止整个进程组并清理 reader task |
| 输出超过上限 | 截断展示，保留已读总量信息 |
| 受保护路径或 hard link | 在 guard/seatbelt 层拒绝或限制，不启动命令 |
| 非 macOS | 不启用 Seatbelt；不可依赖它承担通用策略 |

## 自测题

1. 为什么取消时必须处理进程组而不是只调用 `proc.kill()`？
2. 为什么 Seatbelt 不能取代 ToolGuard？
