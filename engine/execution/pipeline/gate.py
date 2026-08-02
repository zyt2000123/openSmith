"""Gate framework for pipeline quality checks.

The engine owns only the *mechanism*: the ``Gate`` protocol, the
``GateResult`` contract, and the ``LLMGate`` wrapper that layers LLM
semantic verification over a cheap heuristic pre-filter.

Concrete gate implementations are content, not engine code.  They live
under ``agents/gates/<domain>/`` and are registered at startup by
``engine.execution.pipeline.skill_chain.load_gate_content``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol

from engine.llm.observability import llm_purpose

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    verdict: Literal["pass", "fail", "retry"]
    reason: str
    retry_hint: str | None = None


class Gate(Protocol):
    async def check(self, output: str, context: dict) -> GateResult | object: ...


def coerce_gate_result(value: object) -> GateResult:
    """Adapt declarative content decisions without importing engine types there."""
    if isinstance(value, GateResult):
        verdict, reason, retry_hint = value.verdict, value.reason, value.retry_hint
    elif isinstance(value, Mapping):
        verdict = value.get("verdict")
        reason = value.get("reason")
        retry_hint = value.get("retry_hint")
    else:
        verdict = getattr(value, "verdict", None)
        reason = getattr(value, "reason", None)
        retry_hint = getattr(value, "retry_hint", None)

    # ``GateResult`` is a plain dataclass whose ``Literal`` verdict is not
    # runtime-enforced, so it must pass the same checks as every other shape.
    if verdict not in {"pass", "fail", "retry"} or not isinstance(reason, str):
        raise TypeError("gate must return verdict=pass|fail|retry and a string reason")
    if retry_hint is not None and not isinstance(retry_hint, str):
        raise TypeError("gate retry_hint must be a string when provided")
    return GateResult(verdict, reason, retry_hint=retry_hint)


def _normalized_llm_verdict(text: str) -> str:
    """Upper-case a gate verdict, tolerating benign trailing punctuation.

    A strict ``text == "PASS"`` match turns ``"PASS."`` or ``"PASS:"`` into an
    invalid-verdict failure with a retry.  The gate system is deliberately
    strict about *content*, but verdict formatting is not signal: models often
    emit a trailing period or colon after the verdict word.
    """
    return text.strip().rstrip(".!?。！？:：;；\t\r\n").upper()


class LLMGate:
    """LLM-based semantic verification layer on top of a heuristic pre-filter."""

    def __init__(self, inner: Gate, prompt_template: str):
        self._inner = inner
        self._prompt_template = prompt_template
        self._llm = None  # set via set_llm()

    def set_llm(self, llm):
        self._llm = llm

    async def check(self, output: str, context: dict) -> GateResult:
        # First run the heuristic pre-filter
        result = coerce_gate_result(await self._inner.check(output, context))
        if result.verdict == "fail":
            return result  # pre-filter already caught it, no need for LLM

        # Pre-filter passed — now verify semantically with LLM
        if not self._llm:
            return GateResult(
                "fail",
                "LLM verification unavailable",
                retry_hint="Retry after the gate LLM becomes available.",
            )

        try:
            prompt = self._prompt_template.format(output=output[:2000])
            with llm_purpose("gate"):
                resp = await self._llm.chat([
                    {"role": "system", "content": "You are a quality gate. Evaluate the output and respond with ONLY 'PASS' or 'FAIL: <reason>'. Be strict."},
                    {"role": "user", "content": prompt},
                ])
            text = resp.text.strip()
            verdict = _normalized_llm_verdict(text)
            if verdict.startswith("FAIL"):
                reason = text[len("FAIL"):].strip(":： \t\r\n")
                return GateResult("fail", f"LLM verification: {reason}", retry_hint=reason)
            if verdict.startswith("PASS"):
                return result
            return GateResult(
                "fail",
                "LLM verification returned an invalid verdict",
                retry_hint="Retry the semantic verification.",
            )
        except Exception:
            logger.warning(
                "gate LLM verification failed; failing the gate",
                exc_info=True,
            )
            return GateResult(
                "fail",
                "LLM verification failed",
                retry_hint="Retry after the gate LLM becomes available.",
            )
