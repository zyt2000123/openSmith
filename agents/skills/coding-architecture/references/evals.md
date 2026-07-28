# coding-architecture minimal evals

- **trigger** — 计划涉及 API、Engine 和 Shell；应说明文件职责、调用流和依赖。
- **route** — 单文件文案修正；应跳过本阶段，由条件直接进入实施。
- **happy-path** — 三个以上文件变更；输出应能画出请求到验证的完整数据流。
- **guard** — 发现公开接口变化；应列出兼容性与迁移风险，不能直接开始破坏性修改。
