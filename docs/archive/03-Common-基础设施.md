# 03 · Common 基础设施

> **已归档 —— 不是当前事实。**
> 本文已被 [40 · Common 基础设施](../layers/40-Common.md) 取代；两者冲突时以那一篇和源码为准。
> 裁决依据：探针 6:6 平局，取更新的一篇（08-15 vs 08-06），且覆盖更详尽。
> 保留在此仅供追溯当时的设计取舍，不再随代码更新。


本文档描述 `common/` 当前对上层暴露的基础设施契约：路径、私有运行目录、SQLite 连接、YAML 配置，以及随安装包分发的内置技能资源。

---

## 1. 模块定位与设计原则

`common/` 是 Agent-Smith 四层架构的最底层基础设施。它的职责是为上层提供：

- **路径管理** — 项目根目录和数据目录的定位与路径派生
- **配置常量** — 上层模块所需的全局路径常量
- **SQLite 连接管理** — 异步数据库连接的单例与生命周期
- **YAML 工具** — 配置文件的安全读写与深度合并
- **哈希链审计日志** — 防篡改的 JSONL 追加日志原语，供 engine 的追踪与安全审计使用
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
├── config.py         # 路径常量再导出 + reset_paths() + ensure_dirs()
├── paths.py          # AppPaths 数据类，路径派生逻辑核心
├── database.py       # SQLite 异步连接管理（单例模式）
├── yaml_utils.py     # YAML 读写、深度合并、原子写入
├── hash_chain.py     # 防篡改哈希链 JSONL 审计日志（HashChainLog）
└── pyproject.toml    # 包元信息与依赖声明
```

（`uv.lock` 锁文件省略。）

---

## 3. paths.py — 路径管理核心

### 3.1 模块级常量

| 常量 | 值 | 含义 |
|------|-----|------|
| `PROJECT_ROOT_ENV` | `"AGENT_SMITH_PROJECT_ROOT"` | 环境变量名，用于显式指定项目根目录 |
| `PRIVATE_DIR_MODE` | `0o700` | 目录权限模式。Owner 可读/可写/可执行，其他用户无任何权限 |
| `PRIVATE_FILE_MODE` | `0o600` | 私有文件权限。Owner 可读/可写，其他用户无任何权限 |

### 3.2 `_default_project_root() -> Path`

私有函数，用于确定项目根目录。采用环境变量、源码位置和 cwd 搜索三层策略；若都不能定位完整运行时资源，则明确失败而不是返回一个不可用目录。

**第一优先：环境变量**

```python
configured_root = os.environ.get(PROJECT_ROOT_ENV)
```

若设置了 `AGENT_SMITH_PROJECT_ROOT` 环境变量：
1. 对路径做 `expanduser()` + `resolve()` 得到绝对路径
2. 校验该路径具备 Smith 标记：`agents/smith/config.yaml`、`agents/identities/smith.yaml` 和至少一个 `agents/skills/*/SKILL.md`，否则抛出 `RuntimeError`
3. 校验通过则返回该路径

**第二优先：源码位置推断**

```python
source_root = Path(__file__).resolve().parent.parent
```

取 `paths.py` 所在目录（`common/`）的父目录。仅当该目录同时包含以下 Smith 标记时，才认定为项目根目录并返回：

- `agents/smith/config.yaml`
- `agents/identities/smith.yaml`
- 至少一个 `agents/skills/*/SKILL.md`

这是最常见的命中路径 — 在开发环境中从源码目录运行时，`common/` 的父目录就是仓库根。

**第三优先：向上遍历工作目录**

```python
working_dir = Path.cwd().resolve()
for candidate in (working_dir, *working_dir.parents):
    if not (candidate / "agents").is_dir():
        continue
    if _is_agent_smith_root(candidate):
        return candidate
```

从当前工作目录开始逐级向父目录搜索，找到第一个具备全部 Smith 标记（`_is_agent_smith_root()`）的目录即返回。单独存在通用的 `agents/` 目录不会命中，避免误将其他项目当作 Smith 根目录；命中 `agents/` 但缺标记的候选会记 debug 日志，便于诊断根目录发现失配。

**兜底**

如果三层策略全部未命中，抛出 `RuntimeError` 并提示设置 `AGENT_SMITH_PROJECT_ROOT`。这尤其适用于只安装了 `common` wheel 的场景：该 wheel 只携带内置技能 data files，不携带完整的 `agents/` 运行时资产，不能把 `site-packages` 当作项目根目录。

### 3.3 `_ensure_private_dir(path: Path) -> None`

私有函数，创建新的私有目录并拒绝不安全的路径类型：

```python
def _ensure_private_dir(path: Path) -> None:
    _ensure_real_path(path, label="private runtime path")
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(...)
        return
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)
```

`_ensure_real_path()` 会逐段检查已有祖先和路径本身，拒绝任何符号链接，因此不会通过 `data_dir` 的符号链接父目录在外部创建运行时文件。新建目录后会调用 `chmod()`，避免 `mkdir()` 的 `mode` 被 umask 放宽。已有的真实目录保留原权限，不会被静默收紧；已有普通文件会触发 `NotADirectoryError`。

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
def defaults(cls) -> AppPaths:
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

显式确保三个基础数据目录存在（新建目录的权限为 `0o700`，已有真实目录保持现有权限），随后同步内置技能：

1. `~/.agent-smith/` — 数据根目录
2. `~/.agent-smith/agent/` — Agent 实例目录
3. `~/.agent-smith/sqlite/` — SQLite 数据库所在目录

技能同步（`_install_builtin_skills()`）：

4. 额外创建 `~/.agent-smith/builtin/` 与 `~/.agent-smith/builtin/skills/`（同为 `0o700`）
5. 从 `bundled_skills_dir` 中找出包含顶层 `SKILL.md` 的目录，复制到 `~/.agent-smith/builtin/skills/`
6. 删除该目标目录中不再属于当前分发集合的技能目录，并写入权限为 `0o600` 的 `.manifest.json`

wheel 安装时，分发来源是 `sysconfig.get_path("data")` 下的 `agent_smith_common/builtin_skills/`；源码开发时才回退到仓库的 `agents/skills/`。若分发来源不存在，技能同步安全地跳过；若来源存在但发现的技能集合为空、且目标目录已装有技能，则记 warning 并整体跳过本次同步，不删除任何已装技能（防止空/损坏的分发包抹掉内置技能）。`agent/skills/` 仍保留给用户安装的技能，不会被此同步覆盖。

`.manifest.json` 记录每个分发文件的 source/target `mtime_ns`、`size` 和源文件 SHA-256。两端元数据均未变化时，后续同步不会重新读取文件内容；任一元数据变化时才计算 SHA-256 并按内容决定是否复制。这样可以恢复普通篡改，同时保留重复启动的低 I/O 路径；刻意伪造时间戳和大小的攻击不在该元数据快路径的完整性保证内。

数据目录及分发目标中的符号链接始终不被跟随：`data_dir` 的任一既有祖先为符号链接时初始化直接失败；若链接占用了本次分发应写入的位置，同步会失败；若它只是 stale 文件或过期技能目录，则只 `unlink()` 链接本身，不会触及外部目标。

---

## 4. config.py — 路径常量再导出与初始化

`config.py` 是上层模块引用路径的主入口。它将 `AppPaths` 延迟初始化，并为旧的模块级常量名称提供兼容访问：

1. 首次访问时创建并缓存一个 `AppPaths.defaults()` 实例
2. 通过模块级 `__getattr__` 从当前实例派生 `PATHS`、`DATA_DIR` 等兼容名称
3. 提供 `reset_paths()` 和 `ensure_dirs()`

### 4.1 模块级常量

```python
from .paths import AppPaths

_paths_instance: AppPaths | None = None

def _get_paths() -> AppPaths:
    global _paths_instance
    if _paths_instance is None:
        _paths_instance = AppPaths.defaults()
    return _paths_instance

def __getattr__(name: str):
    paths = _get_paths()
    # PATHS and legacy derived-path names are mapped here.
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

`reset_paths(paths: AppPaths | None = None)` 可替换缓存实例，供测试或运行时重配置使用；不传或传 `None` 表示清空缓存，下次属性访问时惰性重建默认实例。通过 `config.PATHS`、`config.DATA_DIR` 等模块属性访问的代码会观察到替换后的值；`from common.config import PATHS` 遵循 Python 导入绑定语义，会保留导入时的快照，不能用来观察之后的 `reset_paths()`。

### 4.2 `ensure_dirs() -> None`

```python
def ensure_dirs() -> None:
    _get_paths().ensure_base_dirs()
```

委托给 `AppPaths.ensure_base_dirs()`。上层可直接调用它确保数据目录就绪；`database.py` 在首次连接时从当前 `config.PATHS` 取得 `AppPaths`，并在线程中直接调用该实例的方法，避免阻塞事件循环。

### 4.3 设计考量

为什么不直接让上层 `from common.paths import AppPaths` ？

- **简化消费方代码** — `from common.config import SQLITE_PATH` 比 `AppPaths.defaults().sqlite_path` 更简洁
- **惰性单例语义** — 首次访问才创建 `AppPaths`，随后模块属性访问共享缓存实例；`reset_paths()` 是有意的替换点
- **兼容性** — 上层已大量使用 `from common.config import ...`，此模块作为稳定的公开接口

---

## 5. database.py — SQLite 异步连接管理

### 5.1 模块级状态

```python
_db: aiosqlite.Connection | None = None
_db_path: Path | None = None
_db_lock = asyncio.Lock()
_db_init_lock = asyncio.Lock()
```

- `_db` — 单例连接引用，初始为 `None`
- `_db_path` — 缓存连接对应的 SQLite 路径；路径切换时旧连接会被关闭
- `_db_lock` — 保护连接引用、路径和健康检查
- `_db_init_lock` — 串行化目录准备与新连接创建，避免并发初始化重复工作

### 5.2 `get_db() -> aiosqlite.Connection`

```python
async def get_db() -> aiosqlite.Connection:
```

获取全局 SQLite 连接。流程如下：

```
解析当前 config.PATHS 和 sqlite_path
  └─ 同路径缓存连接 → 执行 SELECT 1
      ├─ 成功：返回缓存连接
      └─ 失败：关闭并清除缓存
  └─ 无可用缓存 → 进入初始化锁并再次检查
      └─ 在线程中准备目录和同步技能
      └─ 创建、配置并缓存新连接
```

**连接初始化流程：**

1. 在线程中调用 `paths.ensure_base_dirs()` 确保 `~/.agent-smith/sqlite/` 目录存在，并避免技能哈希/复制阻塞事件循环；此步骤不持有 `_db_lock`
2. `aiosqlite.connect(str(sqlite_path))` 创建连接
3. 设置 `db.row_factory = aiosqlite.Row` — 查询结果以 `Row` 对象返回（支持按列名访问）
4. 执行 `PRAGMA journal_mode=WAL` — 启用 Write-Ahead Logging，允许读写并发
5. 执行 `PRAGMA foreign_keys=ON` — 启用外键约束（SQLite 默认关闭外键）
6. 执行 `PRAGMA busy_timeout=5000` — Server 与 Shell 共享数据库文件时，写竞争最多等待 5 秒而非立即报 `database is locked`
7. 缓存连接每次返回前执行轻量 `SELECT 1` 探活；失效连接自动关闭并重建
8. 若初始化过程中任何步骤抛异常，立即 `await db.close()` 关闭连接后重新抛出

### 5.3 `close_db() -> None`

```python
async def close_db() -> None:
```

关闭全局连接并将 `_db` 置为 `None`：

1. 获取初始化锁，避免关闭操作与新连接创建交错
2. 检查 `_db is None` — 若已关闭则直接返回
3. 将 `_db` 引用取出、置 `None`，`_db_path` 一并置 `None`（保证路径切换后能干净重连）
4. 调用 `await db.close()`

先置 `None` 再 `close()` 的顺序确保后续调用不会返回一个正在关闭的连接。`_db_lock` 保护缓存状态，`_db_init_lock` 则保证关闭完成后才会开始下一次连接创建。

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
- `save_yaml`: 写入数据不是 mapping

### 6.3 `_ensure_private_parent(path: Path) -> None`

私有函数，确保指定父目录可安全写入：

```python
def _ensure_private_parent(path: Path) -> None:
    _ensure_real_path(path, label="YAML path")  # 拒绝路径链中的符号链接
    # 只创建缺失的目录；已有目录保持权限
    ...
```

它复用 `paths.py` 的 `_ensure_real_path()` 逐段检查已有父目录，任一符号链接都会抛出 `RuntimeError`，从而避免 `mkstemp(dir=...)` 经由链接写到外部位置。`save_yaml()` 会额外检查目标文件本身。只对新建目录设置 `0o700`；已有目录保留既有权限；普通文件占据父目录位置时抛出 `NotADirectoryError`。

### 6.4 `load_yaml(path: Path | str) -> dict[str, Any]`

安全加载 YAML 配置文件：

```python
def load_yaml(path: Path | str) -> dict[str, Any]:
```

**行为：**

1. 将参数转为 `Path` 对象
2. 若目标不是常规文件（不存在、是目录或特殊文件）→ 返回空字典 `{}`（不报错）
3. 以 UTF-8 编码打开文件
4. 使用 `yaml.safe_load()` 解析（`safe_load` 不执行任意 Python 对象构造，防止代码注入）
5. 解析结果为 `None`（空文件或纯注释文件）→ 返回空字典 `{}`
6. 解析结果不是 `dict` → 抛出 `YamlConfigError`
7. 解析成功 → 返回字典

**保证：**
- 返回值类型始终为 `dict[str, Any]`
- 目标不是常规文件或内容为空时不抛异常，返回空字典
- YAML 格式错误时抛 `YamlConfigError`（包装了原始 `yaml.YAMLError`）
- 根节点为列表、标量等非字典类型时抛 `YamlConfigError`

### 6.5 `save_yaml(path: Path | str, data: Any) -> None`

原子写入 YAML 文件：

```python
def save_yaml(path: Path | str, data: Any) -> None:
```

**原子写入流程：**

1. 拒绝目标文件或父链中的符号链接，并要求 `data` 为 mapping
2. 调用 `yaml.safe_dump(data, allow_unicode=True, sort_keys=False)` 序列化
   - `allow_unicode=True` — 中文等非 ASCII 字符直接输出，不转义
   - `sort_keys=False` — 保持字典键的插入顺序
3. 调用 `_ensure_private_parent(p.parent)` 确保父目录存在且可安全写入
4. 在同目录下创建临时文件（`tempfile.mkstemp`）
   - 前缀为 `.<原文件名>.`
   - 后缀为 `.tmp`
   - 同目录确保后续 `os.replace()` 是同文件系统操作（原子性保证）
5. 将序列化内容写入临时文件
6. 调用 `f.flush()` + `os.fsync(f.fileno())` 确保数据落盘
7. 设置临时文件权限为 `0o600`（Owner 可读可写）
8. 调用 `os.replace(temp_path, p)` 原子替换目标文件
9. 若任何步骤失败，删除临时文件（`temp_path.unlink(missing_ok=True)`）后重新抛出异常

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
- **顶层返回新字典，浅拷贝语义** — 合并过程不就地修改任何输入字典；但仅对双方均为 `dict` 的键递归重建，其余值（含列表与仅单侧出现的嵌套 `dict`）按引用共享进结果。调用方若要就地修改返回值的嵌套结构，需自行 `deepcopy` 以免污染输入配置
- **递归深度无限制** — 嵌套字典无论多深都会递归合并

**典型用法：**

```python
# 基础配置 + 用户配置 + 运行时覆盖
final = merge_configs(default_config, user_config, runtime_overrides)
```

---

## 7. hash_chain.py — 防篡改哈希链审计日志

为 engine 的追踪与安全审计提供可校验的追加式 JSONL 日志原语。消费方：`engine/observability/trace_store.py` 与 `engine/safety/tool_guard.py`。

### 7.1 模块级常量与纯函数

| 常量/函数 | 含义 |
|------|------|
| `CHAIN_VERSION = 1` | 链格式版本号 |
| `CHAIN_FILE_MODE = 0o600`、`_PRIVATE_DIR_MODE = 0o700` | 日志文件/目录权限（模块内自行定义，未复用 `paths.py` 的同值常量——已知技术债） |
| `canonical_json(value)` | 键序稳定的规范化 JSON 序列化 |
| `sha256_hex(text)` | SHA-256 十六进制摘要 |
| `genesis_hash(namespace)` | 按 namespace 派生创世哈希 |
| `record_hash(record)` | 单条记录的链哈希 |

### 7.2 `HashChainLog`

追加式 JSONL 日志，每条记录携带 `seq` / `prev_hash` / `hash`，逐条链式绑定：

- `append(record, *, sync=False)` — 追加一条记录并写入链字段
- `seal()` — 将链头密封写入旁路 anchor 文件 `<log>.head`（用于检测整链回滚/截断）
- `verify(anchor=None)` — 校验整链完整性，返回 `ChainVerification`（frozen dataclass）
- `unseal()` / `close()` / `ensure_handle()` / `file_handle` — 生命周期与句柄管理

**legacy 尾部处理**：对既有的无链纪录文件，链只绑定其最后一条记录，且首条链式记录必须显式声明 `legacy_linked: true`——校验方据此区分"合法迁移"与"降级攻击"，未声明的 legacy 拼接会校验失败。

**多写者防护**：`_reload_if_externally_appended()` 检测其他进程对同一日志的追加，避免误报篡改。

### 7.3 `verify_chain(path, *, namespace, anchor=None)`

模块级校验入口，独立于写入方重放整条链并核对 anchor。

---

## 8. `__init__.py`

空文件。仅使 `common/` 成为 Python 包，不导出任何符号。

上层模块的标准导入方式是：

```python
from common.config import SQLITE_PATH, DATA_DIR, ensure_dirs
from common.database import get_db, close_db
from common.yaml_utils import load_yaml, save_yaml, merge_configs
from common.paths import AppPaths  # 需要自定义路径时
from common.hash_chain import HashChainLog, verify_chain  # 审计日志
```

---

## 9. 依赖方向

### 9.1 内部依赖

```
config.py ──import──→ paths.py
database.py ──import──→ config.py ──import──→ paths.py
yaml_utils.py ──import──→ paths.py（复用私有权限常量和符号链接防护）
hash_chain.py（叶子模块，仅依赖标准库）
```

`yaml_utils.py` 不依赖配置或数据库层，只复用 `paths.py` 中的权限常量和路径安全辅助函数。`hash_chain.py` 是 common 内的第二个叶子模块，零 common 内部依赖；它自行重复定义了 `0o600`/`0o700` 权限常量而非从 `paths.py` 导入，属已知的常量重复。

### 9.2 谁依赖 common

| 消费方 | 导入内容 | 用途 |
|--------|---------|------|
| `engine/` | `config.py` 路径常量、`database.py` 连接管理、`yaml_utils.py` 配置工具、`paths.py`（直接构造 `AppPaths`、私有权限常量）、`hash_chain.py`（trace_store、tool_guard 审计链） | 记忆存储、Agent 配置加载、LLM 配置读取、可观测性与安全审计 |
| `server/` | 同上（`paths.py` 私有权限常量用于 auth、skill_service），加上 `ensure_dirs()` | 启动时初始化目录、数据库连接、配置文件管理 |
| `agents/` | 主体是纯内容层不 import；唯一例外是可执行钩子 `agents/smith/hooks/cost_tracker.py` 直接 `from common.config import DATA_DIR`。纯内容文件（skills/identities/tools 定义）不 import common | 被 engine 按路径读取；hooks 由 HookLoader 动态加载执行 |

### 9.3 common 不得依赖的模块

- `engine/` — Agent 框架层，比 common 高一级
- `server/` — 平台后端层，最高级
- `agents/` — 内容层，由 engine 按路径消费

违反此规则会引入循环依赖，破坏分层架构。

---

## 10. 与其他层的接口契约

### 10.1 engine 对 common 的期望

| 契约 | 具体要求 |
|------|---------|
| 路径稳定性 | 同一缓存 `AppPaths` 实例的派生路径稳定；`reset_paths()` 是显式重配置边界 |
| 数据库可用性 | `get_db()` 返回已启用 WAL、外键和 5 秒 busy timeout 的可探活连接；缓存失效会自动重连 |
| YAML 安全性 | `load_yaml()` 使用 `safe_load`，`save_yaml()` 拒绝目标及父链中的符号链接 |
| 合并确定性 | `merge_configs()` 的覆盖语义一致，`None` 值不覆盖 |
| 目录就绪 | 调用 `ensure_dirs()` 后，`data_dir`、`agent_dir`、`sqlite/` 目录已存在；新建目录为私有权限，可用分发资源会同步到 `builtin/skills/` |

### 10.2 server 对 common 的期望

| 契约 | 具体要求 |
|------|---------|
| 连接生命周期 | `close_db()` 安全关闭连接，支持 FastAPI 的 shutdown 事件 |
| 幂等初始化 | `ensure_dirs()` 和 `get_db()` 可多次调用；过期 managed 符号链接会被安全移除 |
| 原子写入 | `save_yaml()` 不会在崩溃时产生半写文件，也不会跟随目标或父链符号链接 |

### 10.3 common 不提供的东西

- **Schema 管理** — common 不负责创建或迁移数据库表；当前表结构由 `server/app/infrastructure/schema.py` 管理
- **配置校验** — common 只解析 YAML 为字典，不校验配置内容的业务语义
- **Agent/Session 概念** — common 的路径命名（如 `agent_dir`）只是字符串，不含业务含义

---

## 11. pyproject.toml — 依赖与分发资源

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

### 11.1 内置技能 data files

`[tool.setuptools.data-files]` 将每个内置技能的 `SKILL.md` 以及需要随技能分发的引用文件写入 wheel 的 `agent_smith_common/builtin_skills/` 目录。安装后的 `bundled_skills_dir` 优先读取该位置，因此源码目录存在技能并不等于安装包已经包含它。该 `common` wheel 不携带完整的 `agents/smith`、`agents/identities`、tools 或 safety 资源；脱离源码树运行时必须通过 `AGENT_SMITH_PROJECT_ROOT` 指向完整资源根目录。

新增内置技能或其引用文件时，必须同步更新该清单；`server/tests/test_common_infrastructure.py`（`test_wheel_data_files_reproduce_every_bundled_skill_file`）用 `rglob` 遍历每个技能内的**每一个文件**（含 `references/` 等子目录），与声明的 data-files 做**双向精确集合比对**——漏写或多写任一引用文件都会导致测试失败，不止校验技能本体。

---

## 12. 验证与维护

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

验证重点是：私有目录/文件权限、managed/YAML 符号链接边界、manifest 增量同步、缓存连接探活与异步初始化、无效 YAML 的错误边界、原子写入，以及 wheel 中的内置技能资源。
