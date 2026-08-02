# Gates — pipeline 门禁内容层

门禁实现是**内容**,不是引擎代码。引擎只负责调用约定、将内容层返回的
`verdict` / `reason` / `retry_hint` 适配为内部结果，并在有 `llm_prompt` 时自动
注入 LLM gate。具体检查规则住在这里，按领域分目录组织，由
`engine.execution.skill_chain.load_gate_content()` 启动时扫描注册。

## 目录约定

```
agents/gates/common/    跨领域通用门禁(understanding / planning / review …)
agents/gates/coding/    coding 领域门禁(test / rubric / pr / git_worktree …)
agents/gates/<domain>/  新领域按需添加,不改引擎
agents/conditions/      步骤条件(pipeline YAML 的 condition: 键)
```

## 编写一个门禁

任意 `.py` 文件(`_` 开头的除外),导出模块级 `GATES` 字典:

```python
class GateResult:
    def __init__(self, verdict: str, reason: str, retry_hint: str | None = None):
        self.verdict = verdict
        self.reason = reason
        self.retry_hint = retry_hint

class MyGate:
    async def check(self, output: str, context: dict) -> GateResult:
        ok = "关键证据" in output
        return GateResult(
            "pass" if ok else "retry",
            "说明原因",
            retry_hint="不过时给 LLM 的重试提示",
        )

GATES = {"my_gate": MyGate}
```

- 不要从 `engine`、`server` 或 `common` 导入。内容文件可直接调用运行时注入的
  `output_key("planning")` 读取某个步骤产物。
- `GateResult.verdict`: `"pass"` 通过 / `"retry"` 同节点重试 / `"fail"` 走回退或阻断
- 需要 LLM 语义复核时，在 gate 上声明 `llm_prompt = "..."`；引擎会自动包装并注入 gate LLM
- 条件文件同理,导出 `CONDITIONS = {"key": fn}`,`fn(ctx: dict) -> bool`

## pipeline YAML 引用

```yaml
base_gate: rubric        # 兜底层:每个节点产出先过它(可选,可为列表 base_gates)
steps:
  - skill: diagnosing-bugs
    gate: red_loop        # 领域层:该节点自己的门禁
    condition: coding_bugfix_needs_diagnosis   # 可选
```

键名冲突(两个文件注册同一 key)与内容文件语法错误都会在启动时直接抛错——
宁可启动失败,不许静默丢门禁。
