"""Domain-neutral quality gates shared by every pipeline.

Content layer: loaded by ``engine.execution.skill_chain.load_gate_content``.
The module-level ``GATES`` mapping is merged into the gate registry that
pipeline YAML ``gate:`` / ``base_gate:`` keys resolve against.  Adding a
gate here (or in a sibling domain directory) requires no engine change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    """Content-only gate decision; the engine adapts this duck-typed value."""

    verdict: str
    reason: str
    retry_hint: str | None = None


class UnderstandingGate:
    """Check that understanding output restates the requirement and identifies boundaries.

    QoderWake: Understand 阶段末 — 能准确复述需求 + 识别边界条件；失败回退重新理解。
    """

    _RESTATEMENT = re.compile(
        r"需求|目标|任务是|要求|用户(想|希望|需要)|goal|requirement|objective|"
        r"the task is|user wants?|needs? to",
        re.IGNORECASE,
    )

    _BOUNDARIES = re.compile(
        r"边界|约束|前提|假设|范围|不包括|不涉及|限制|风险|"
        r"boundar|constraint|assumption|edge case|out of scope|scope|limitation|risk",
        re.IGNORECASE,
    )

    async def check(self, output: str, context: dict) -> GateResult:
        stripped = output.strip()
        missing: list[str] = []

        # 与 SkillRubricGate 一致用 50：中文信息密度高，80 会误伤正常输出
        if len(stripped) <= 50:
            missing.append(f"substantive understanding (only {len(stripped)} chars)")
        if not self._RESTATEMENT.search(stripped):
            missing.append("restatement of the requirement")
        if not self._BOUNDARIES.search(stripped):
            missing.append("boundary conditions / constraints / assumptions")

        if not missing:
            return GateResult("pass", "Understanding restates the requirement and identifies boundaries.")

        return GateResult(
            "fail",
            f"Understanding output missing: {', '.join(missing)}.",
            retry_hint=(
                "Restate the requirement in your own words, then explicitly list boundary "
                "conditions, constraints, and assumptions (至少 2 条边界/约束)."
            ),
        )


class PlanningGate:
    """Check that output has numbered steps (>= 3) and verification points."""

    async def check(self, output: str, context: dict) -> GateResult:
        # Count numbered steps: "1." or "1、" at line start (with optional whitespace)
        steps = re.findall(r"^\s*\d+[.、)]\s", output, re.MULTILINE)
        num_steps = len(steps)

        has_verify = bool(re.search(
            r"验证|检查|确认|verify|check|test",
            output,
            re.IGNORECASE,
        ))

        if num_steps >= 3 and has_verify:
            return GateResult("pass", f"Plan has {num_steps} numbered steps and verification points.")

        missing = []
        if num_steps < 3:
            missing.append(f"at least 3 numbered steps (found {num_steps})")
        if not has_verify:
            missing.append("verification points")
        return GateResult(
            "retry",
            f"Planning output missing: {', '.join(missing)}.",
            retry_hint="Add a verification checkpoint after each step (e.g., '验证: 运行测试确认...'). Ensure at least 3 numbered steps.",
        )


class PlanningLLMGate(PlanningGate):
    llm_prompt = (
        "Verify this plan is substantive (not boilerplate). Does it have concrete, actionable "
        "steps specific to the task? Does each step have a real verification point?\n\n"
        "Plan output:\n{output}"
    )


class ReviewGate:
    """Check for issue categorization and actionable findings."""

    async def check(self, output: str, context: dict) -> GateResult:
        has_categorization = bool(re.search(
            r"P0|P1|P2|critical|major|minor|severity|"
            r"严重|主要|次要|优先级",
            output,
            re.IGNORECASE,
        ))

        has_finding = bool(re.search(
            r"(issue|finding|problem|bug|concern|建议|问题|发现)",
            output,
            re.IGNORECASE,
        ))

        has_no_issues = bool(re.search(
            r"no\s*(issues?|problems?|findings?)\s*found|"
            r"(looks?\s*good|LGTM|approved)|"
            r"没有(发现)?(问题|缺陷)",
            output,
            re.IGNORECASE,
        ))

        if has_categorization and (has_finding or has_no_issues):
            return GateResult("pass", "Review includes categorized findings or explicit clean bill.")

        missing = []
        if not has_categorization:
            missing.append("issue categorization (P0/P1/P2 or critical/major/minor)")
        if not has_finding and not has_no_issues:
            missing.append("at least one actionable finding OR explicit 'no issues found'")
        return GateResult(
            "retry",
            f"Review output missing: {', '.join(missing)}.",
            retry_hint="Categorize each finding by severity (P0/P1/P2 or critical/major/minor). If no issues, explicitly state 'no issues found'.",
        )


class ContractAlignmentGate:
    """Check that the implementation approach is verified against the plan/contract.

    QoderWake: 实现前 — 实现方案与契约一致；失败回退 Planning。
    """

    _ALIGNMENT_VERDICT = re.compile(
        r"一致|对齐|符合|无偏差|偏差|不一致|aligned|consistent|match(es)?|deviation|conform",
        re.IGNORECASE,
    )

    _CONCRETE_REF = re.compile(
        r"```|[\w./-]+\.\w{1,5}|第\s*\d+\s*步|step\s*\d+|\d+\.\s",
    )

    async def check(self, output: str, context: dict) -> GateResult:
        stripped = output.strip()
        missing: list[str] = []

        if not self._ALIGNMENT_VERDICT.search(stripped):
            missing.append("explicit alignment verdict (一致/偏差/aligned/deviation)")
        if not self._CONCRETE_REF.search(stripped):
            missing.append("concrete references to plan items or files")
        has_planning_context = any(
            key.endswith("planning_output") and bool(value)
            for key, value in context.items()
        )
        if not has_planning_context and "计划" not in stripped and "plan" not in stripped.lower():
            missing.append("reference to the plan being aligned against")

        if not missing:
            return GateResult("pass", "Implementation approach is checked against the plan/contract.")

        return GateResult(
            "fail",
            f"Contract alignment missing: {', '.join(missing)}.",
            retry_hint=(
                "Compare the implementation approach item-by-item against the plan: cite each "
                "plan step or file, state 一致/偏差 per item, and give an overall verdict."
            ),
        )


GATES = {
    "understanding": UnderstandingGate,
    "planning": PlanningGate,
    "planning_llm": PlanningLLMGate,
    "review": ReviewGate,
    "contract_alignment": ContractAlignmentGate,
}
