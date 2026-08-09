"""UnderstandingGate / ContractAlignmentGate 单测 + 技能链接线检查。"""
import asyncio
import sys
from pathlib import Path

import pytest

from engine.execution.pipeline.gate import GateResult, LLMGate, coerce_gate_result
from engine.execution.pipeline.skill_chain import GATE_REGISTRY, load_gate_content
from engine.llm.client import ChatResponse


ROOT = Path(__file__).resolve().parents[3]
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


def _llm_gate_with_verdict(raw_text: str):
    class PassingGate:
        async def check(self, output, context):
            return GateResult("pass", "heuristic pass")

    class VerdictLLM:
        async def chat(self, messages):
            return ChatResponse(text=raw_text)

    gate = LLMGate(PassingGate(), "check {output}")
    gate.set_llm(VerdictLLM())
    return asyncio.run(gate.check("output", {}))


def test_llm_gate_accepts_case_insensitive_pass_with_trailing_punctuation():
    """Benign formatting must not turn a valid PASS into an invalid-verdict retry."""
    for raw in ("PASS", "PASS.", "PASS:", "pass", "Pass。", "PASS:"):
        result = _llm_gate_with_verdict(raw)
        assert result.verdict == "pass", raw


def test_llm_gate_parses_fail_and_carries_the_reason():
    result = _llm_gate_with_verdict("FAIL: evidence is missing")
    assert result.verdict == "fail"
    assert "evidence is missing" in result.reason


def test_llm_gate_accepts_literal_braces_in_the_prompt_template():
    """A gate template containing JSON samples or other literal braces must
    not make the check raise (and silently fail forever)."""

    class RetryGate:
        async def check(self, output, context):
            return GateResult("retry", "heuristic uncertain")

    class VerdictLLM:
        async def chat(self, messages):
            return ChatResponse(text="PASS")

    gate = LLMGate(RetryGate(), 'Check {output} against {"required": "evidence"}')
    gate.set_llm(VerdictLLM())
    result = asyncio.run(gate.check("output", {}))

    assert result.verdict == "pass"


def test_llm_gate_pass_escalates_a_heuristic_retry_to_pass():
    """When the heuristic pre-filter is uncertain (retry) but the LLM then
    explicitly verifies the output, the gate must pass — the LLM verdict must
    be able to de-escalate retry, not only escalate fail."""

    class RetryGate:
        async def check(self, output, context):
            return GateResult("retry", "heuristic uncertain")

    class VerdictLLM:
        async def chat(self, messages):
            return ChatResponse(text="PASS")

    gate = LLMGate(RetryGate(), "check {output}")
    gate.set_llm(VerdictLLM())
    result = asyncio.run(gate.check("output", {}))

    assert result.verdict == "pass"
    assert "LLM verification passed" in result.reason


def test_coerce_gate_result_validates_gate_result_instances():
    """A concrete GateResult must not bypass verdict/reason validation."""
    with pytest.raises(TypeError):
        coerce_gate_result(GateResult("maybe", ""))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        coerce_gate_result(GateResult("pass", 42))  # type: ignore[arg-type]
    assert coerce_gate_result(GateResult("pass", "ok")).verdict == "pass"


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


# ── Review: a gate must fail closed on an ambiguous verdict ──


def test_llm_gate_fails_when_the_verdict_mentions_fail_after_pass() -> None:
    """``startswith("PASS")`` was checked after ``startswith("FAIL")``, so a reply
    that restates the choice before deciding ("PASS/FAIL determination: FAIL")
    matched PASS and let a rejected output through."""

    class _PreFilter:
        async def check(self, output, context):
            return GateResult("pass", "heuristic ok")

    class _LLM:
        async def chat(self, messages, **kwargs):
            return ChatResponse(text="PASS/FAIL determination: FAIL - no tests were run")

    gate = LLMGate(_PreFilter(), "check {output}")
    gate.set_llm(_LLM())
    result = asyncio.run(gate.check("some output", {}))

    assert result.verdict == "fail"
    assert "no tests were run" in result.reason
    assert result.retry_hint == "no tests were run"


def test_llm_gate_still_passes_a_plain_pass_verdict() -> None:
    class _PreFilter:
        async def check(self, output, context):
            return GateResult("pass", "heuristic ok")

    class _LLM:
        async def chat(self, messages, **kwargs):
            return ChatResponse(text="PASS.")

    gate = LLMGate(_PreFilter(), "check {output}")
    gate.set_llm(_LLM())

    assert asyncio.run(gate.check("some output", {})).verdict == "pass"


def test_llm_gate_reports_a_reason_for_a_lowercase_fail() -> None:
    class _PreFilter:
        async def check(self, output, context):
            return GateResult("pass", "heuristic ok")

    class _LLM:
        async def chat(self, messages, **kwargs):
            return ChatResponse(text="fail: missing migration")

    gate = LLMGate(_PreFilter(), "check {output}")
    gate.set_llm(_LLM())
    result = asyncio.run(gate.check("some output", {}))

    assert result.verdict == "fail"
    assert result.retry_hint == "missing migration"


def test_llm_gate_shows_the_model_the_end_of_a_long_output() -> None:
    """Both gate layers must judge the same evidence.

    The heuristic pre-filter scans the whole output while the LLM layer sees a
    bounded copy.  Verification nodes put their evidence last -- the command
    output they just produced -- so a head-only bound let the pre-filter
    confirm evidence the model could not see, failing a correct node.
    """
    seen: list[str] = []

    class _PreFilter:
        async def check(self, output, context):
            # What the real ValidationGate does: scan the entire output.
            assert "12 passed" in output
            return GateResult("pass", "heuristic ok")

    class _LLM:
        async def chat(self, messages, **kwargs):
            seen.append(messages[-1]["content"])
            return ChatResponse(text="PASS")

    long_output = (
        "开始执行验证流程。\n"
        + "中间过程日志，逐条记录每一步操作。\n" * 400
        + "最终测试结果：12 passed, 0 failed"
    )
    assert len(long_output) > 2000

    gate = LLMGate(_PreFilter(), "Verify this report:\n{output}")
    gate.set_llm(_LLM())
    result = asyncio.run(gate.check(long_output, {}))

    assert result.verdict == "pass"
    prompt = seen[0]
    assert "12 passed" in prompt, "the gate LLM never saw the evidence at the end"
    assert "开始执行验证流程" in prompt, "the gate LLM lost the start of the output"
    assert "omitted from gate input" in prompt, "truncation must be explicit"
