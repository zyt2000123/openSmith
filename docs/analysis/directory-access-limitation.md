# Smith 目录访问限制分析与解决方案

**问题**: Smith 启动后只能访问当前工作目录，无法访问其他目录（如用户的其他项目、文档等）

**日期**: 2026-08-02  
**状态**: 已分析，待解决

---

## 问题根源

### 1️⃣ 核心限制代码

**文件**: `engine/safety/tool_guard.py`

#### 工作目录设置（第 183-188 行）
```python
def set_working_directory(self, working_dir: Path) -> None:
    root = Path(working_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"working directory does not exist: {working_dir}")
    self._working_dir = root
    self.set_allowed_dirs([root])  # ← 只允许当前目录
```

#### 默认允许目录（第 177-181 行）
```python
def set_allowed_dirs(self, allowed_dirs: list[Path] | None) -> None:
    if allowed_dirs:
        self._allowed = [p.resolve() for p in allowed_dirs]
    else:
        # 默认：家目录 + /tmp + 当前目录
        self._allowed = [Path.home().resolve(), Path("/tmp").resolve(), Path.cwd().resolve()]
```

#### 路径检查逻辑（第 365-387 行）
```python
# 如果路径在允许列表中 → 通过
if any(target.is_relative_to(d) for d in self._allowed):
    return GuardResult(allowed=True)

# 如果是项目作用域模式 → 需要审批
if self.is_working_directory_scoped:
    return self._path_approval(
        target,
        writing=writing,
        reason=(
            f"Path {path_str} is outside the active working directory "
            "and requires explicit user approval"
        ),
        boundary_block=True,  # ← 标记为边界阻止
    )
```

### 2️⃣ 问题流程

```
用户启动 Smith 在 /Users/alice/project-a
         ↓
server 调用 set_working_directory("/Users/alice/project-a")
         ↓
FileGuard._allowed = ["/Users/alice/project-a"]  # ← 覆盖默认
         ↓
Smith 尝试访问 /Users/alice/documents/notes.md
         ↓
check_path() 发现路径不在 _allowed 中
         ↓
返回 GuardResult(boundary_block=True, approval_required=True)
         ↓
❌ 需要用户审批才能访问
```

---

## 设计意图（安全考虑）

这个限制是**有意设计**的安全边界：

### ✅ 优点
1. **最小权限原则** - Agent 只能访问明确授权的目录
2. **防止意外修改** - 避免 Agent 误操作其他项目或系统文件
3. **审计清晰** - 明确知道 Agent 的活动范围
4. **安全沙箱** - 限制潜在的恶意行为或模型错误

### ⚠️ 缺点
1. **灵活性不足** - 合法的跨目录需求被阻止
2. **用户体验差** - 每次访问其他目录都需要审批
3. **工作流中断** - 无法流畅地处理多项目任务

---

## 现有的白名单机制

**好消息**: Smith **已经实现**了会话级白名单系统！

### SessionWhitelist 类（第 463-501 行）

```python
class SessionWhitelist:
    def __init__(self) -> None:
        self._allowed_tools: set[str] = set()
        self._allowed_paths: set[str] = set()   # ← 目录白名单
        self._allowed_files: set[str] = set()   # ← 文件白名单

    def allow_path(self, path: str) -> None:
        """添加允许的目录"""
        self._allowed_paths.add(str(Path(path).resolve()))

    def allow_file(self, path: str) -> None:
        """添加允许的文件"""
        self._allowed_files.add(str(Path(path).resolve()))

    def is_path_allowed(self, path: str) -> bool:
        """检查路径是否在白名单中"""
        resolved = Path(path).resolve()
        if str(resolved) in self._allowed_files:
            return True
        for p in self._allowed_paths:
            base = Path(p)
            try:
                if resolved == base or resolved.is_relative_to(base):
                    return True
            except ValueError:
                continue
        return False
```

### 白名单检查（第 741-744 行）

```python
# 如果是边界阻止且在会话白名单中 → 允许
if result.boundary_block and self.whitelist.is_path_allowed(p):
    continue  # 跳过审批
```

---

## 解决方案

### 方案 A：动态审批 + 会话白名单（推荐）✅

**原理**: 首次访问时审批，审批后加入会话白名单，后续自动允许

**优点**:
- ✅ 安全性高（首次需审批）
- ✅ 用户体验好（后续自动）
- ✅ 已有基础设施

**实现步骤**:

1. **用户首次访问外部目录**
   ```
   Smith: 我需要访问 /Users/alice/documents/notes.md
   用户: [批准]
   → 自动调用 tool_guard.whitelist.allow_path("/Users/alice/documents")
   ```

2. **后续访问同目录自动通过**
   ```
   Smith: 访问 /Users/alice/documents/other.md
   → whitelist.is_path_allowed() 返回 True
   → 无需再次审批
   ```

3. **实现代码修改**

   **文件**: `server/app/services/session_service.py` 或审批处理模块

   ```python
   async def handle_approval(self, approval: ToolApproval):
       # 现有审批逻辑
       if approval.approved:
           # 新增：如果是路径审批，加入白名单
           if approval.scope.type == "path":
               path = Path(approval.scope.path)
               if path.is_dir():
                   # 目录：整个目录加入白名单
                   self.tool_guard.whitelist.allow_path(str(path))
               else:
                   # 文件：父目录加入白名单（或只允许该文件）
                   self.tool_guard.whitelist.allow_path(str(path.parent))
   ```

### 方案 B：预配置信任目录 🔧

**原理**: 配置文件中预先声明常用目录

**配置示例**: `~/.agent-smith/config.yaml`

```yaml
llm:
  provider: openai
  api_key: sk-...

# 新增：信任目录配置
trusted_directories:
  - ~/Documents
  - ~/projects
  - ~/workspace
  - /tmp
```

**实现**:

```python
# common/config.py 或 engine/safety/tool_guard.py
def load_trusted_directories() -> list[Path]:
    config = load_config()
    dirs = config.get("trusted_directories", [])
    return [Path(d).expanduser().resolve() for d in dirs]

# FileGuard 初始化时加载
def set_working_directory(self, working_dir: Path) -> None:
    root = Path(working_dir).expanduser().resolve()
    self._working_dir = root
    
    # 扩展允许目录：工作目录 + 信任目录
    trusted = load_trusted_directories()
    self.set_allowed_dirs([root, *trusted])
```

### 方案 C：临时扩展命令 🎯

**原理**: 提供命令让用户临时授权目录访问

**示例**:

```bash
smith> /trust ~/Documents
✓ Added ~/Documents to session whitelist

smith> /trust --list
Trusted directories this session:
- /Users/alice/Documents
- /Users/alice/workspace

smith> /trust --clear
✓ Cleared session whitelist
```

**实现**:

```typescript
// shell/src/commands.ts
"/trust": async (args, context) => {
  const path = args.trim();
  if (!path) {
    // 列出当前白名单
    const list = await context.bridge.getTrustedPaths();
    state.set({ statusLine: `Trusted: ${list.join(", ")}` });
    return;
  }
  
  // 添加到白名单
  await context.bridge.trustPath(path);
  state.set({ statusLine: `✓ Added ${path} to session whitelist` });
}
```

---

## 推荐实施方案

### 🎯 分阶段实施

**Phase 1: 动态审批增强（立即实施）**
- 修改审批处理逻辑，审批后自动加入会话白名单
- 无需配置，开箱即用
- 代码改动最小（约 10 行）

**Phase 2: 配置文件支持（短期）**
- 添加 `trusted_directories` 配置项
- 启动时预加载信任目录
- 减少常用目录的审批次数

**Phase 3: CLI 命令（可选）**
- 添加 `/trust` 命令
- 提供更灵活的运行时控制
- 增强用户体验

---

## 安全审查

### ✅ 保持的安全边界

1. **高风险路径仍需审批**: `.ssh/`, `.aws/`, `.env` 等始终需要高风险审批
2. **硬链接检测**: 防止通过硬链接绕过保护
3. **符号链接解析**: 检查实际路径和词法路径
4. **会话隔离**: 白名单是会话级别，不跨会话持久化（除非配置）

### ⚠️ 潜在风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 用户盲目批准所有请求 | 首次访问显示清晰的路径和权限说明 |
| 白名单范围过大 | 只添加实际访问的路径，不自动扩展到父目录 |
| 配置文件被篡改 | 配置文件本身受 tool_guard 保护，需高风险审批 |
| 会话劫持 | 白名单不持久化，重启后重新授权 |

---

## 实现优先级

**立即实施** (Phase 1):
```python
# 修改 1 个文件，约 10-15 行代码
# 文件: server/app/services/approval_handler.py (或类似)

async def on_approval_granted(self, approval_scope: ApprovalScope):
    if approval_scope.type == "path":
        path = Path(approval_scope.path)
        # 添加父目录到白名单
        self.tool_guard.whitelist.allow_path(str(path.parent))
        logger.info(f"Added {path.parent} to session whitelist")
```

**测试场景**:
1. ✅ Smith 访问 `/Users/alice/docs/file.txt` → 审批
2. ✅ Smith 再访问 `/Users/alice/docs/other.txt` → 自动通过
3. ✅ Smith 访问 `/Users/alice/.ssh/id_rsa` → 仍然高风险审批
4. ✅ 重启 Smith → 白名单清空，重新授权

---

## 总结

Smith 的目录访问限制是**设计良好的安全特性**，但缺少**便利性机制**。

**现状**: 有完整的白名单基础设施，但未充分利用  
**问题**: 每次跨目录访问都需手动审批  
**解决**: 审批后自动加入会话白名单（10行代码）

**建议**: 立即实施 Phase 1，用户体验将显著改善，安全性不降低。
