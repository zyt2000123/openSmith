# 03 · Common 基础设施

本文档描述 `common/` 当前对上层暴露的基础设施契约：路径、私有运行目录、SQLite 连接、YAML 配置，以及随安装包分发的内置技能资源。

---

## 1. 模块定位与设计原则

`common/` 是 Agent-Smith 四层架构的最底层基础设施。它的职责是为上层提供：

- **路径管理** — 项目根目录和数据目录的定位与路径派生
- **配置常量** — 上层模块所需的全局路径常量
- **SQLite 连接管理** — 异步数据库连接的单例与生命周期
- **YAML 工具** — 配置文件的安全读写与深度合并
- **内置技能资源** — 从安装包或源码树发现、同步 Smith 自带技能到私有运行目录

### 1.1 零业务逻辑原则

`common/` 不包含 Agent、Session、Memory、路由或技能执行等业务逻辑。它只提供文件系统路径、数据库连接和配置解析工具；对 `SKILL.md` 的识别仅用于同步随 Smith 分发的文件资源，不解析技能语义也不参与路由。

### 1.2 禁止上向依赖

`common/` 不得 import `engine/`、`server/`、`agents/` 中的任何内容。依赖方向是严格单向的：

```
server/ ──import──→ engine/ ──import──→ common/
                               ↑
                    agents/（读取内容）
```

`common/` 是叶子节点，只依赖第三方库（`pyyaml`、`aiosqlite`）和 Python 标准库。

---

## 2. 文件结构

```
common/
├── __init__.py       # 空文件，使 common/ 成为 Python 包
├── config.py         # 路径常量再导出 + ensure_dirs()
├── paths.py          # AppPaths 数据类，路径派生逻辑核心
├── database.py       # SQLite 异步连接管理（单例模式）
├── yaml_utils.py     # YAML 读写、深度合并、原子写入
└── pyproject.toml    # 包元信息与依赖声明
```

---

## 3. paths.py — 路径管理核心

### 3.1 模块级常量

| 常量 | 值 | 含义 |
|------|-----|------|
| `PROJECT_ROOT_ENV` | `"AGENT_SMITH_PROJECT_ROOT"` | 环境变量名，用于显式指定项目根目录 |
| `PRIVATE_DIR_MODE` | `0o700` | 目录权限模式。Owner 可读/可写/可执行，其他用户无任何权限 |
| `PRIVATE_FILE_MODE` | `0o600` | 私有文件权限。Owner 可读/可写，其他用户无任何权限 |

### 3.2 `_default_project_root() -> Path`

私有函数，用于确定项目根目录。采用三级回退策略：

**第一优先：环境变量**

```python
configured_root = os.environ.get(PROJECT_ROOT_ENV)
```

若设置了 `AGENT_SMITH_PROJECT_ROOT` 环境变量：
1. 对路径做 `expanduser()` + `resolve()` 得到绝对路径
2. 校验该路径下必须存在 `agents/` 子目录，否则抛出 `RuntimeError`
3. 校验通过则返回该路径

**第二优先：源码位置推断**

```python
source_root = Path(__file__).resolve().parent.parent
```

取 `paths.py` 所在目录（`common/`）的父目录。若该目录下存在 `agents/` 子目录，则认定为项目根目录并返回。

这是最常见的命中路径 — 在开发环境中从源码目录运行时，`common/` 的父目录就是仓库根。

**第三优先：向上遍历工作目录**

```python
working_dir = Path.cwd().resolve()
for candidate in (working_dir, *working_dir.parents):
    if (candidate / "agents").is_dir():
        return candidate
```

从当前工作目录开始，逐级向父目录搜索，找到第一个包含 `agents/` 子目录的目录即返回。

**兜底**

如果三级策略全部未命中，返回 `source_root`（第二优先级计算的路径）。此时 `agents/` 可能不存在，但至少有一个确定性路径可用。

### 3.3 `_ensure_private_dir(path: Path) -> None`

私有函数，创建目录并强制设置 `0o700` 权限：

```python
def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)
```

注意：调用 `mkdir()` 后再次调用 `chmod()` 是因为 `mkdir()` 的 `mode` 参数会被进程的 umask 修改。显式 `chmod()` 确保权限一定是 `0o700`，不受 umask 影响。

### 3.4 `AppPaths` 数据类

```python
@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    project_root: Path
```

`frozen=True` 表示实例不可变 — 一旦创建，`data_dir` 和 `project_root` 不可被修改。

#### 3.4.1 构造方法

**`defaults() -> AppPaths`** (classmethod)

```python
@classmethod
def defaults(cls) -> "AppPaths":
    return cls(
        data_dir=Path.home() / ".agent-smith",
        project_root=_default_project_root(),
    )
```

- `data_dir` 固定为 `~/.agent-smith/`
- `project_root` 通过 `_default_project_root()` 三级回退策略确定

#### 3.4.2 派生路径属性

所有属性均为 `@property`，从 `data_dir` 或 `project_root` 派生：

| 属性 | 基于 | 返回路径 | 用途 |
|------|------|---------|------|
| `agent_dir` | `data_dir` | `~/.agent-smith/agent/` | Smith Agent 实例数据目录 |
| `sqlite_path` | `data_dir` | `~/.agent-smith/sqlite/agent-smith.sqlite` | SQLite 数据库文件路径 |
| `smith_profile_dir` | `project_root` | `<repo>/agents/smith/` | Smith 内置身份种子目录 |
| `builtin_identities_dir` | `project_root` | `<repo>/agents/identities/` | YAML 领域身份目录 |
| `builtin_skills_dir` | `data_dir` | `~/.agent-smith/builtin/skills/` | Smith 管理的、已同步的内置技能目录；不属于用户可编辑的 `agent/skills/` |
| `bundled_skills_dir` | 安装包优先，源码树回退 | `<python-data>/agent_smith_common/builtin_skills/` 或 `<repo>/agents/skills/` | 内置技能的只读分发来源 |
| `builtin_tools_dir` | `project_root` | `<repo>/agents/tools/` | 内置工具定义目录 |
| `safety_rules_path` | `project_root` | `<repo>/agents/safety/dangerous_commands.json` | 危险命令安全规则文件 |

路径按归属可分两组：

- **数据侧** (`data_dir` 派生)：`agent_dir`、`sqlite_path`、`builtin_skills_dir` — 用户数据、SQLite 文件和 Smith 管理的运行时技能副本
- **源码侧** (`project_root` 派生)：`smith_profile_dir`、`builtin_identities_dir`、`builtin_tools_dir`、`safety_rules_path` — 仓库内随代码分发的内容
- **分发来源**：`bundled_skills_dir` 优先使用 wheel 安装的 data files；仅在开发源码树中回退到 `agents/skills/`

#### 3.4.3 `ensure_base_dirs() -> None`

```python
def ensure_base_dirs(self) -> None:
    _ensure_private_dir(self.data_dir)
    _ensure_private_dir(self.agent_dir)
    _ensure_private_dir(self.sqlite_path.parent)
    self._install_builtin_skills()
```

确保三个关键数据目录存在且权限为 `0o700`，并同步内置技能：

1. `~/.agent-smith/` — 数据根目录
2. `~/.agent-smith/agent/` — Agent 实例目录
3. `~/.agent-smith/sqlite/` — SQLite 数据库所在目录
4. 从 `bundled_skills_dir` 中找出包含顶层 `SKILL.md` 的目录，复制到 `~/.agent-smith/builtin/skills/`
5. 删除该目标目录中不再属于当前分发集合的技能目录，并写入权限为 `0o600` 的 `.manifest.json`

wheel 安装时，分发来源是 `sysconfig.get_path("data")` 下的 `agent_smith_common/builtin_skills/`；源码开发时才回退到仓库的 `agents/skills/`。若分发来源不存在，技能同步安全地跳过。`agent/skills/` 仍保留给用户安装的技能，不会被此同步覆盖。

---

## 4. config.py — 路径常量再导出与初始化

`config.py` 是上层模块引用路径常量的主入口。它的全部逻辑是：

1. 创建一个 `AppPaths.defaults()` 单例
2. 将所有派生路径展开为模块级常量
3. 提供 `ensure_dirs()` 初始化函数

### 4.1 模块级常量

```python
from .paths import AppPaths

PATHS = AppPaths.defaults()

DATA_DIR = PATHS.data_dir
AGENT_DIR = PATHS.agent_dir
SQLITE_PATH = PATHS.sqlite_path
SMITH_PROFILE_DIR = PATHS.smith_profile_dir
BUILTIN_IDENTITIES_DIR = PATHS.builtin_identities_dir
BUILTIN_SKILLS_DIR = PATHS.builtin_skills_dir
BUILTIN_TOOLS_DIR = PATHS.builtin_tools_dir
SAFETY_RULES_PATH = PATHS.safety_rules_path
```

导出的常量与 `AppPaths` 属性一一对应：

| 常量 | 对应属性 | 典型值 |
|------|---------|--------|
| `PATHS` | — | `AppPaths` 实例本身 |
| `DATA_DIR` | `data_dir` | `~/.agent-smith/` |
| `AGENT_DIR` | `agent_dir` | `~/.agent-smith/agent/` |
| `SQLITE_PATH` | `sqlite_path` | `~/.agent-smith/sqlite/agent-smith.sqlite` |
| `SMITH_PROFILE_DIR` | `smith_profile_dir` | `<repo>/agents/smith/` |
| `BUILTIN_IDENTITIES_DIR` | `builtin_identities_dir` | `<repo>/agents/identities/` |
| `BUILTIN_SKILLS_DIR` | `builtin_skills_dir` | `~/.agent-smith/builtin/skills/` |
| `BUILTIN_TOOLS_DIR` | `builtin_tools_dir` | `<repo>/agents/tools/` |
| `SAFETY_RULES_PATH` | `safety_rules_path` | `<repo>/agents/safety/dangerous_commands.json` |

### 4.2 `ensure_dirs() -> None`

```python
def ensure_dirs() -> None:
    PATHS.ensure_base_dirs()
```

委托给 `AppPaths.ensure_base_dirs()`。上层模块（特别是 `database.py`）在首次连接数据库前调用此函数，确保数据目录就绪。

### 4.3 设计考量

为什么不直接让上层 `from common.paths import AppPaths` ？

- **简化消费方代码** — `from common.config import SQLITE_PATH` 比 `AppPaths.defaults().sqlite_path` 更简洁
- **单例语义** — `PATHS` 在模块加载时创建一次，后续所有 import 共享同一实例
- **兼容性** — 上层已大量使用 `from common.config import ...`，此模块作为稳定的公开接口

---

## 5. database.py — SQLite 异步连接管理

### 5.1 模块级状态

```python
_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
```

- `_db` — 单例连接引用，初始为 `None`
- `_db_lock` — 异步互斥锁，防止并发初始化竞态

### 5.2 `get_db() -> aiosqlite.Connection`

```python
async def get_db() -> aiosqlite.Connection:
```

获取全局 SQLite 连接。采用双重检查锁定（Double-Checked Locking）模式：

```
第一次检查 _db（无锁）
  └─ 非 None → 直接返回
  └─ None → 加锁
      └─ 第二次检查 _db（有锁）
          └─ 非 None → 释放锁，返回
          └─ None → 创建连接
```

**连接初始化流程：**

1. 调用 `ensure_dirs()` 确保 `~/.agent-smith/sqlite/` 目录存在
2. `aiosqlite.connect(str(SQLITE_PATH))` 创建连接
3. 设置 `db.row_factory = aiosqlite.Row` — 查询结果以 `Row` 对象返回（支持按列名访问）
4. 执行 `PRAGMA journal_mode=WAL` — 启用 Write-Ahead Logging，允许读写并发
5. 执行 `PRAGMA foreign_keys=ON` — 启用外键约束（SQLite 默认关闭外键）
6. 执行 `PRAGMA busy_timeout=5000` — Server 与 Shell 共享数据库文件时，写竞争最多等待 5 秒而非立即报 `database is locked`
7. 若初始化过程中任何步骤抛异常，立即 `await db.close()` 关闭连接后重新抛出

### 5.3 `close_db() -> None`

```python
async def close_db() -> None:
```

关闭全局连接并将 `_db` 置为 `None`：

1. 加锁
2. 检查 `_db is None` — 若已关闭则直接返回
3. 将 `_db` 引用取出、置 `None`
4. 调用 `await db.close()`

先置 `None` 再 `close()` 的顺序确保后续调用不会返回一个正在关闭的连接。`_db_lock` 会让发现 `_db is None` 的并发 `get_db()` 等待关闭完成，再串行创建新连接。

### 5.4 WAL 模式说明

WAL (Write-Ahead Logging) 是 SQLite 的日志模式，与默认的 rollback journal 相比：

- **读写并发** — 读操作不阻塞写操作，写操作不阻塞读操作
- **性能** — 写操作更快（顺序写 WAL 文件，不需要复制整页到回滚日志）
- **持久化** — WAL 模式是持久设置，一旦在某个数据库上启用，重新打开该数据库仍为 WAL 模式

Agent-Smith 作为本地单用户应用，WAL 模式的主要收益是允许 FastAPI 的多个异步请求处理器并发读取数据库，同时不阻塞写入操作。

---

## 6. yaml_utils.py — YAML 工具集

### 6.1 模块级常量

| 常量 | 值 | 含义 |
|------|-----|------|
| `PRIVATE_DIR_MODE` | `0o700` | 从 `paths.py` 导入的目录权限常量 |
| `PRIVATE_FILE_MODE` | `0o600` | 从 `paths.py` 导入的私有文件权限常量。Owner 可读可写，其他用户无任何权限 |

### 6.2 `YamlConfigError`

```python
class YamlConfigError(ValueError):
    """Raised when a configuration YAML document is invalid or unsafe to persist."""
```

继承自 `ValueError`。在以下场景抛出：
- `load_yaml`: YAML 解析失败
- `load_yaml`: YAML 根元素不是 mapping（字典）
- `save_yaml`: Python 对象无法序列化为 YAML

### 6.3 `_ensure_private_parent(path: Path) -> None`

私有函数，确保指定路径存在且权限为 `0o700`：

```python
def _ensure_private_parent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)
```

与 `paths.py` 中的 `_ensure_private_dir` 使用同一套权限常量，但封装为文件父目录的初始化操作。调用者传入的是文件所在目录（即 `p.parent`），而非文件路径本身。

### 6.4 `load_yaml(path: Path | str) -> dict[str, Any]`

安全加载 YAML 配置文件：

```python
def load_yaml(path: Path | str) -> dict[str, Any]:
```

**行为：**

1. 将参数转为 `Path` 对象
2. 若文件不存在 → 返回空字典 `{}`（不报错）
3. 以 UTF-8 编码打开文件
4. 使用 `yaml.safe_load()` 解析（`safe_load` 不执行任意 Python 对象构造，防止代码注入）
5. 解析结果为 `None`（空文件或纯注释文件）→ 返回空字典 `{}`
6. 解析结果不是 `dict` → 抛出 `YamlConfigError`
7. 解析成功 → 返回字典

**保证：**
- 返回值类型始终为 `dict[str, Any]`
- 文件不存在或为空时不抛异常，返回空字典
- YAML 格式错误时抛 `YamlConfigError`（包装了原始 `yaml.YAMLError`）
- 根节点为列表、标量等非字典类型时抛 `YamlConfigError`

### 6.5 `save_yaml(path: Path | str, data: Any) -> None`

原子写入 YAML 文件：

```python
def save_yaml(path: Path | str, data: Any) -> None:
```

**原子写入流程：**

1. 调用 `yaml.safe_dump(data, allow_unicode=True, sort_keys=False)` 序列化
   - `allow_unicode=True` — 中文等非 ASCII 字符直接输出，不转义
   - `sort_keys=False` — 保持字典键的插入顺序
2. 调用 `_ensure_private_parent(p.parent)` 确保父目录存在且权限为 `0o700`
3. 在同目录下创建临时文件（`tempfile.mkstemp`）
   - 前缀为 `.<原文件名>.`
   - 后缀为 `.tmp`
   - 同目录确保后续 `os.replace()` 是同文件系统操作（原子性保证）
4. 将序列化内容写入临时文件
5. 调用 `f.flush()` + `os.fsync(f.fileno())` 确保数据落盘
6. 设置临时文件权限为 `0o600`（Owner 可读可写）
7. 调用 `os.replace(temp_path, p)` 原子替换目标文件
8. 若任何步骤失败，删除临时文件（`temp_path.unlink(missing_ok=True)`）后重新抛出异常

**原子性保证：**
- `os.replace()` 在 POSIX 系统上是原子操作
- 写入过程中断电或崩溃，目标文件要么是旧内容（临时文件未 replace），要么是完整新内容（replace 已完成）
- 不会出现半写状态

### 6.6 `merge_configs(*configs: dict[str, Any]) -> dict[str, Any]`

深度合并多个配置字典：

```python
def merge_configs(*configs: dict[str, Any]) -> dict[str, Any]:
    """Deep merge dicts. Later overrides earlier."""
```

**合并语义：**

按参数顺序从左到右合并，后者覆盖前者。逐键处理，规则如下：

| 已有值 (`result[key]`) | 新值 (`value`) | 行为 |
|------------------------|----------------|------|
| 任意 | `None` | **跳过** — `None` 值被忽略，不覆盖已有值 |
| `dict` | `dict` | **递归合并** — 调用 `merge_configs(result[key], value)` |
| `dict` | 非 `dict` | **覆盖** — 新值替换整个字典 |
| 非 `dict` | `dict` | **覆盖** — 新字典替换旧标量/列表 |
| 非 `dict` | 非 `dict` | **覆盖** — 新值替换旧值 |
| 不存在 | 任意非 `None` | **设置** — 新增键 |

**关键细节：**

- **列表不做合并** — 列表被视为标量，直接覆盖而非追加或合并。例如 `{"a": [1,2]}` 和 `{"a": [3,4]}` 合并结果为 `{"a": [3,4]}`
- **`None` 是"不覆盖"标记** — 若某层配置的某个键值为 `None`，该键不会影响合并结果。这允许在叠加配置文件时表达"此项使用默认值"
- **返回新字典** — 不修改任何输入字典，返回全新的合并结果
- **递归深度无限制** — 嵌套字典无论多深都会递归合并

**典型用法：**

```python
# 基础配置 + 用户配置 + 运行时覆盖
final = merge_configs(default_config, user_config, runtime_overrides)
```

---

## 7. `__init__.py`

空文件。仅使 `common/` 成为 Python 包，不导出任何符号。

上层模块的标准导入方式是：

```python
from common.config import SQLITE_PATH, DATA_DIR, ensure_dirs
from common.database import get_db, close_db
from common.yaml_utils import load_yaml, save_yaml, merge_configs
from common.paths import AppPaths  # 需要自定义路径时
```

---

## 8. 依赖方向

### 8.1 内部依赖

```
config.py ──import──→ paths.py
database.py ──import──→ config.py ──import──→ paths.py
yaml_utils.py ──────→ (无内部依赖)
```

`yaml_utils.py` 是完全独立的，不依赖 `common/` 内的其他模块。

### 8.2 谁依赖 common

| 消费方 | 导入内容 | 用途 |
|--------|---------|------|
| `engine/` | `config.py` 路径常量、`database.py` 连接管理、`yaml_utils.py` 配置工具 | 记忆存储、Agent 配置加载、LLM 配置读取 |
| `server/` | 同上，加上 `ensure_dirs()` | 启动时初始化目录、数据库连接、配置文件管理 |
| `agents/` | 不直接 import（纯内容层），但通过 engine 间接消费路径 | 被 engine 按路径读取 |

### 8.3 common 不得依赖的模块

- `engine/` — Agent 框架层，比 common 高一级
- `server/` — 平台后端层，最高级
- `agents/` — 内容层，由 engine 按路径消费

违反此规则会引入循环依赖，破坏分层架构。

---

## 9. 与其他层的接口契约

### 9.1 engine 对 common 的期望

| 契约 | 具体要求 |
|------|---------|
| 路径稳定性 | `SMITH_PROFILE_DIR`、`BUILTIN_SKILLS_DIR` 等路径在进程生命周期内不变 |
| 数据库可用性 | `get_db()` 返回已启用 WAL、外键和 5 秒 busy timeout 的连接 |
| YAML 安全性 | `load_yaml()` 使用 `safe_load`，不会执行危险的 YAML 构造 |
| 合并确定性 | `merge_configs()` 的覆盖语义一致，`None` 值不覆盖 |
| 目录就绪 | 调用 `ensure_dirs()` 后，`data_dir`、`agent_dir`、`sqlite/` 目录已存在且权限正确；可用分发资源会同步到 `builtin/skills/` |

### 9.2 server 对 common 的期望

| 契约 | 具体要求 |
|------|---------|
| 连接生命周期 | `close_db()` 安全关闭连接，支持 FastAPI 的 shutdown 事件 |
| 幂等初始化 | `ensure_dirs()` 和 `get_db()` 可多次调用，不会出错 |
| 原子写入 | `save_yaml()` 不会在崩溃时产生半写文件 |

### 9.3 common 不提供的东西

- **Schema 管理** — common 不负责创建或迁移数据库表；当前表结构由 `server/app/infrastructure/schema.py` 管理
- **配置校验** — common 只解析 YAML 为字典，不校验配置内容的业务语义
- **Agent/Session 概念** — common 的路径命名（如 `agent_dir`）只是字符串，不含业务含义

---

## 10. pyproject.toml — 依赖与分发资源

```toml
[project]
name = "agent-smith-common"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pyyaml>=6.0", "aiosqlite>=0.21"]
```

| 依赖 | 版本要求 | 用途 |
|------|---------|------|
| `pyyaml` | >= 6.0 | YAML 解析与序列化（`yaml_utils.py`） |
| `aiosqlite` | >= 0.21 | SQLite 异步连接（`database.py`） |

构建系统使用 `setuptools>=69`。

Python 版本要求 `>=3.11`（使用了 `X | Y` 联合类型语法等 3.10+ 特性，以及 `from __future__ import annotations`）。

### 10.1 内置技能 data files

`[tool.setuptools.data-files]` 将每个内置技能的 `SKILL.md` 以及需要随技能分发的引用文件写入 wheel 的 `agent_smith_common/builtin_skills/` 目录。安装后的 `bundled_skills_dir` 优先读取该位置，因此源码目录存在技能并不等于安装包已经包含它。

新增内置技能或其引用文件时，必须同步更新该清单；`server/tests/test_common_infrastructure.py` 会比较包含顶层 `SKILL.md` 的源码技能目录与已声明的技能根目录，防止遗漏技能本体。

---

## 11. 验证与维护

修改 `common/` 的路径、配置、数据库或分发资源时，至少执行：

```bash
cd server
uv run pytest tests/test_common_infrastructure.py -q
uv run pytest tests/test_config_service.py -q
```

分发资源有变化时，还应从仓库根目录构建 wheel：

```bash
uv build --wheel common
```

验证重点是：私有目录/文件权限、无效 YAML 的错误边界、原子写入、首次 SQLite 初始化的并发收敛，以及 wheel 中的内置技能资源。
