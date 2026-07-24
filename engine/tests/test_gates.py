"""UnderstandingGate / ContractAlignmentGate 单测 + 技能链接线检查。"""
import asyncio
import sys
from pathlib import Path

from engine.execution.pipeline.gate import GateResult, LLMGate
from engine.execution.pipeline.skill_chain import GATE_REGISTRY, load_gate_content


ROOT = Path(__file__).resolve().parents[2]
load_gate_content(ROOT / "agents")


def _gate(key: str):
    factory = GATE_REGISTRY[key]
    return factory() if callable(factory) else factory


def _check(gate, output, context=None):
    return asyncio.run(gate.check(output, context or {}))


def test_understanding_passes_with_restatement_and_boundaries():
    output = (
        "需求复述：用户希望在聊天输入框支持选择工作目录并拖拽文件。"
        "边界条件：仅本机路径在范围内，远程文件不包括；约束：后端接口保持向后兼容。"
    )
    assert _check(_gate("understanding"), output).verdict == "pass"


def test_understanding_fails_when_no_boundaries():
    output = "需求是给输入框加一个按钮，用户想要更方便的操作，这个功能目标很明确。"
    result = _check(_gate("understanding"), output)
    assert result.verdict == "fail"
    assert "boundary" in result.reason


def test_understanding_fails_on_short_output():
    assert _check(_gate("understanding"), "明白了，就是加个按钮。").verdict == "fail"


def test_contract_alignment_passes_with_verdict_and_refs():
    output = (
        "对照计划逐条检查：第 1 步 修改 task_router.py — 一致；"
        "第 2 步 新增 gate.py 类 — 一致。总体结论：与计划一致，可以继续。"
    )
    ctx = {"planning_output": "1. 修改 task_router.py 2. 新增 gate.py 类"}
    assert _check(_gate("contract_alignment"), output, ctx).verdict == "pass"


def test_contract_alignment_fails_without_verdict():
    output = "我看了一下实现方案，感觉整体还行，没有什么大问题。"
    result = _check(_gate("contract_alignment"), output, {"planning_output": "1. xxx"})
    assert result.verdict == "fail"


def test_llm_gate_fails_closed_when_llm_is_unavailable():
    class PassingGate:
        async def check(self, output, context):
            return GateResult("pass", "heuristic pass")

    result = asyncio.run(LLMGate(PassingGate(), "check {output}").check("output", {}))

    assert result.verdict == "fail"
    assert "unavailable" in result.reason


def test_llm_gate_fails_closed_when_llm_verification_errors():
    class PassingGate:
        async def check(self, output, context):
            return GateResult("pass", "heuristic pass")

    class BrokenLLM:
        async def chat(self, messages):
            raise RuntimeError("provider down")

    gate = LLMGate(PassingGate(), "check {output}")
    gate.set_llm(BrokenLLM())
    result = asyncio.run(gate.check("output", {}))

    assert result.verdict == "fail"
    assert result.reason == "LLM verification failed"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
