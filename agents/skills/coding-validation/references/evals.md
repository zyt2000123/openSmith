# coding-validation minimal evals

- **trigger** — 已完成代码改动；应运行目标测试并报告命令、输出和通过/失败数量。
- **route** — 仅要求写一个测试计划；应转回 planning，而不是伪造执行结果。
- **happy-path** — `pytest tests/test_auth.py` 返回 `3 passed`；应报告实际结果和覆盖范围。
- **guard** — 测试依赖生产凭证；应说明阻断和未验证范围，不将其写成通过。
