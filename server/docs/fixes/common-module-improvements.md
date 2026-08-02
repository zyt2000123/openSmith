# common/ 模块改进修复报告

**分支**: `fix/bug`  
**日期**: 2026-08-02  
**测试状态**: ✅ 9/9 通过

## 问题与修复总结

基于对 common/ 模块的深入分析，识别并修复了 4 个关键问题：

---

## 1️⃣ 技能同步优化（高优先级）✅

### 问题
- `_install_builtin_skills()` 每次启动都执行全量 `copytree`
- `.manifest.json` 只写入不读取，无增量判断
- 19 个技能每次启动都触发全量文件 IO

### 修复方案
**文件**: `common/paths.py:96-145`

- **增量复制**：基于 mtime + size 判断文件是否变化
- **利用 manifest**：记录每个文件的元数据（mtime, size）
- **仅复制变化文件**：首次启动后，后续启动跳过未变化文件
- **保留清理逻辑**：仍然删除 stale 文件和过期技能

### 收益
- 首次启动：行为不变（全量复制）
- 后续启动：仅复制变化文件，大幅减少 IO
- 19 个技能场景下，启动时间预计减少 70-90%

---

## 2️⃣ 配置可配置性改进（中优先级）✅

### 问题
- `PATHS = AppPaths.defaults()` 在模块导入时求值
- 运行时修改环境变量无效，必须重启进程
- 测试时需要 monkeypatch 绕过，可测性差

### 修复方案
**文件**: `common/config.py:1-47`

- **延迟初始化**：改为 `_get_paths()` 函数，首次访问时才求值
- **提供重置接口**：`reset_paths()` 支持测试/运行时重配置
- **向后兼容**：通过 `__getattr__` 实现模块级属性懒加载
- **保留常量访问**：`SQLITE_PATH` 等常量仍可直接 import

### 示例
```python
# 测试场景：运行时改环境变量
import os
os.environ["AGENT_SMITH_PROJECT_ROOT"] = "/custom/path"
from common.config import reset_paths
reset_paths()  # 重新加载配置

# 现在 PATHS 会指向新路径
from common.config import PATHS
assert PATHS.project_root == Path("/custom/path")
```

---

## 3️⃣ 数据库健康检查（中优先级）✅

### 问题
- `get_db()` 返回缓存连接，不验证可用性
- 若连接已关闭/损坏，直接返回导致后续操作失败
- 缺少自动重连机制

### 修复方案
**文件**: `common/database.py:13-45`

- **健康检查函数**：`_check_connection_health()` 使用 `PRAGMA quick_check`
- **返回前验证**：从缓存返回连接前先检查健康状态
- **自动重连**：检测到损坏连接时，自动关闭并重建
- **新连接验证**：创建新连接后立即执行健康检查

### 收益
- 防止使用损坏的数据库连接
- 自动从连接故障中恢复
- 无需手动重启进程

---

## 4️⃣ 路径解析鲁棒性增强（低优先级）✅

### 问题
- cwd 向上搜索只检查 `agents/` 目录存在性
- 可能误匹配其他项目的 `agents/` 目录
- 缺少 Agent-Smith 特征验证

### 修复方案
**文件**: `common/paths.py:15-47`

- **严格验证**：检查 Agent-Smith 特征文件：
  - `agents/smith/` 身份目录
  - `agents/identities/` 目录
  - `agents/skills/*/SKILL.md` 技能标记
- **调试日志**：跳过候选目录时记录调试信息
- **保留兜底**：验证失败时仍返回 source_root

### 收益
- 降低误匹配风险
- 提供调试信息辅助排查
- 保持向后兼容（仍有兜底机制）

---

## 测试验证

所有 9 个 `test_common_infrastructure.py` 测试通过：

```bash
✅ test_app_paths_create_private_runtime_dirs_and_exposes_builtin_identities
✅ test_app_paths_honors_explicit_project_root
✅ test_app_paths_installs_shipped_skills_separately_from_user_skills
✅ test_app_paths_reconciles_existing_builtin_skill_directory
✅ test_wheel_data_files_reproduce_every_bundled_skill_file
✅ test_yaml_requires_a_mapping_and_preserves_private_atomic_file
✅ test_yaml_surfaces_invalid_documents_and_unsafe_values
✅ test_config_service_returns_422_for_an_invalid_config
✅ test_get_db_initializes_once_for_concurrent_callers
```

---

## 向后兼容性

所有修复保持向后兼容：

- **API 不变**：`from common.config import SQLITE_PATH` 仍然有效
- **行为一致**：首次启动、正常场景下行为与之前相同
- **仅优化路径**：仅在重复启动、故障恢复、边缘场景下表现更优

---

## 未修复项

以下问题被判定为"可接受"，不需要修复：

1. **data-files 手工维护** - 已有测试强制同步，维护成本可控
2. **UnicodeDecodeError 分层抛出** - 设计合理，非缺陷

---

## 建议后续工作

1. 添加技能同步性能指标（监控实际节省的 IO 时间）
2. 为 `reset_paths()` 添加单元测试
3. 监控数据库健康检查的性能影响（PRAGMA quick_check 开销）
