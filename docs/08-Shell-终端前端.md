# 08 · Shell 终端前端

> 本文描述当前源码已接入的 Shell；功能是否存在以注册、调用路径和回归测试为准。
> Shell 的版本来自 `shell/package.json`，当前为 `0.3.1`。

## 1. 定位与责任边界

`shell/` 是 Agent-Smith 的终端原生前端。它使用 Ink/React 呈现交互界面，使用
Zustand 保存前端状态，通过 HTTP/SSE 连接 `server/`。

```text
terminal
  │ Ink / React
  ▼
shell ── HTTP + SSE ──► server ──► engine
```

Shell 负责：输入、面板、会话/技能选择、流式转录、状态 HUD 和本地开发服务器启动。

Shell 不负责：任务路由、工具安全、审批决策、模型调用、记忆或 Agent 生命周期。
UI 组件不直接 import `engine/`，也不应把显示文本反向当作执行或安全事实。

## 2. 技术栈与质量命令

| 项目 | 当前选择 |
| --- | --- |
| 包 | `smith-shell@0.3.1`，ESM，Node.js 22+ |
| 终端 UI | Ink 6 + React 19 |
| 状态 | Zustand 5 (`zustand/vanilla` + React selector) |
| 正文 Markdown | `@assistant-ui/react-ink-markdown` |
| GFM 表格 AST | `marked` |
| 宽度计算 | `string-width` |
| 代码高亮 | Shiki / `@assistant-ui/react-ink-markdown` helper |
| 静态检查 | Biome |
| 测试 | TypeScript + `node:test`，同目录 `*.test.ts(x)` |

在 `shell/` 下执行：

```bash
npm run check
npm run build
npm test
npm pack --dry-run
```

`npm test` 先做 TypeScript 类型检查，再由 `scripts/run-tests.mjs` 运行编译产物；
脚本在测试报告完成后强制退出，以隔离第三方 Markdown 渲染器遗留的 MessagePort。

## 3. 运行生命周期

```text
smith CLI
  → bin/smith.js
  → dist/index.js / SmithApp
  → NodeBridge.boot()
      → ensureLocalServer()
      → 读取配置、Agent、sessions、skills、MCP
      → store.hydrate()

Composer input
  → useShellInput()
  → submitChat() / runShellCommand() / submitSetup()
  → NodeBridge
  → api.ts streamMessage() / streamRunResume()
  → SSE StreamEvent
  → store.applyEvent()
  → TranscriptEntryView / StatusHud / ShellFooter
```

`bridge.ts` 是所有后端交互的唯一入口。它拥有取消控制器、请求批处理、会话恢复、
技能开关、token/observability 刷新和审批提交；`api.ts` 只定义 HTTP/SSE 协议和
超时控制。`dev-server.ts` 先检查健康与 OpenAPI 所需操作，必要时才启动本地 FastAPI。

## 4. 状态与键盘路由

`store.ts` 中 `AppState` 是 Shell 的单一应用状态：模式、当前面板、转录、输入、
队列、技能、MCP、token、可恢复 Run、审批、token dashboard 和 observability 数据。

面板枚举是：

```text
welcome | chat | sessions | skill-actions | skills | skill-toggle |
mcp | hooks | hook-details | tokens | runs
```

`input.ts` 是唯一键盘行为边界，负责：

- Composer、slash palette 与 `@` skill picker；
- list 的上下/翻页/Home/End 选择；
- Skills、hooks、token tabs、model picker、审批和 Escape/Ctrl-C；
- 输入锁定、排队消息和焦点恢复。

`list-navigation.ts` 只计算索引和可视窗口；它不渲染行。展示组件不得自行监听键盘，
以免产生第二套焦点系统。

## 5. 组件结构

```text
SmithApp
├── Static: HeroPanel + 已完成的 TranscriptEntry
├── ShellContent
│   ├── setup → SetupPanel
│   ├── runs → RunExplorerPanel
│   ├── panels → sessions / skills / skill-toggle / mcp / hooks / tokens
│   └── active TranscriptEntry
└── ShellFooter
    ├── status / pending skill / queued input / running progress
    ├── approval 或 model picker
    ├── Composer + slash / skill mention overlay
    └── StatusHud
```

`ShellContent` 只根据 `mode` 和 `panel` 选择组件。面板的视觉共有层由以下小原语提供：

| 原语 | 职责 | 不负责 |
| --- | --- | --- |
| `PanelContainer` | 标题、说明、内容、底部操作提示的统一边界 | 状态、输入、网络请求 |
| `TabbedPanel` | 当前 tab 与提示的视觉状态 | 左右键路由或 tab 状态修改 |
| `MultiSelectList` | 勾选、焦点、可视窗口与更多提示的渲染 | 搜索、选择、持久化 |

`TokenStatsPanel` 使用 `TabbedPanel`；`SkillTogglePanel` 使用 `MultiSelectList`；
Setup、sessions、skills、MCP、runs 等面板使用 `PanelContainer`。选择、过滤和保存仍
分别由 `input.ts`、`list-navigation.ts`、Store 与 `NodeBridge` 驱动。

## 6. Transcript 与结构化渲染

转录状态在 `transcript-state.ts`，渲染在 `transcript.tsx`。完成的 entry 通过 Ink
`Static` 只写入一次 scrollback；当前 streaming turn 仍在动态区重绘。

```text
SSE event
  → transcript-state 归并为 Turn / Tool / Skill / Thinking / Smith UI block
  → TranscriptEntryView
      → MarkdownMessage
          → splitStreamingMarkdown
          → 代码 / diff / 表格 / 常规 Markdown
```

### Markdown、表格、diff

- 常规正文继续由 `MarkdownText` 显示；`DisplayText` 用于需要显式、显示宽度感知换行
  的终端文本。
- `MarkdownTableBlock` 使用 `marked` AST，按真实终端显示宽度分配列宽，逐单元格强制
  换行，保留 CJK、emoji、URL、长 ID/路径的完整字符。表格始终保持 Unicode 网格，不做
  纵向卡片/字段回退；物理上无法容纳最小网格时显式记录 `overflowed`，不静默删除内容。
- `DiffBlock` 解析 unified diff，显示行号、增加/删除颜色、词级高亮和续行缩进；它也使用
  内容保留型换行。
- `splitStreamingMarkdown` 会把未闭合表格或 fenced block 保留在动态区，直到结构完整再
  固化，避免一行 SSE 就把半张表写死到 scrollback。

这条渲染路径只消费文本和显示宽度；它不改变 SSE、Bridge、Store 或 Engine 的执行语义。

## 7. 可见命令与运行时面板

`commands.ts` 注册当前可见命令，包括 `/new`、`/reload`、`/init`、`/clear`、
`/compress`、`/model`、`/config`、`/sessions`、`/token`、`/runs`、`/trace`、
`/skills`、`/hooks`、`/mcp`、`/resume`、`/compact` 和 `/exit`。

具体能力边界：

- `/skills` 展示可启用技能、运行某个技能或进入启用/停用列表；disabled skill 不会进入
  `@` 选择器。
- `/mcp` 是已配置 MCP 服务及其工具的只读视图；Smith 当前是 MCP client，不在此暴露
  MCP server。
- `/token` 显示本地 token 统计及 tab；`/runs` 与 `/trace` 显示可观测性投影及诊断。
- `/resume` 恢复中断 Run 或打开指定 session；它不替换引擎的恢复策略。

## 8. 扩展规则

1. 新的展示组件先确定它只是 UI，还是需要新增真实 SSE/Engine 语义；两者不能混写。
2. 需要列表导航时复用 `moveListIndex()`/`getVisibleList()`，不把选择逻辑塞进渲染组件。
3. 需要文本布局时复用 `text-layout.ts` 的显示宽度工具，并为 CJK、URL、无空格长 token
   和窄宽度添加回归测试。
4. 流式结构必须先定义“何时稳定”，不能在不完整的表格/diff/fence 上提前写入 Static。
5. 新面板应优先组合 `PanelContainer`、`TabbedPanel`、`MultiSelectList`；只有第三个独立
   使用点验证了重复的交互需求，才扩大原语的 API。

关于哪些外部 CLI 的思路值得借鉴、哪些必须自研，以及开发过程中的人工验收关卡，见
[Shell 组件采纳决策与人工开发评审](research/2026-07-22-shell-component-adoption.md)。
