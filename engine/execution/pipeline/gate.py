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
import re
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


_MAX_GATE_OUTPUT_CHARS = 2000


def _bounded_gate_output(output: str) -> str:
    """Keep both ends of a long node output when feeding the gate LLM.

    The two layers of a gate used to read different text: the heuristic
    pre-filter scans the whole output, while this wrapper passed only its
    first characters to the model.  Verification and review nodes put their
    evidence last -- the command output they just produced -- so the
    pre-filter would confirm evidence the LLM could not see, and the gate
    rejected a node whose output was in fact correct.  Mirrors
    ``engine.memory._review._truncate_source``: keep both ends, and say so.
    """
    if len(output) <= _MAX_GATE_OUTPUT_CHARS:
        return output
    marker = "\n[... middle of output omitted from gate input ...]\n"
    available = _MAX_GATE_OUTPUT_CHARS - len(marker)
    if available <= 0:  # pragma: no cover - marker is far shorter than the budget
        return output[:_MAX_GATE_OUTPUT_CHARS]
    head = available // 2
    return f"{output[:head]}{marker}{output[-(available - head):]}"


def _normalized_llm_verdict(text: str) -> str:
    """Upper-case a gate verdict, tolerating benign trailing punctuation.

    A strict ``text == "PASS"`` match turns ``"PASS."`` or ``"PASS:"`` into an
    invalid-verdict failure with a retry.  The gate system is deliberately
    strict about *content*, but verdict formatting is not signal: models often
    emit a trailing period or colon after the verdict word.
    """
    return text.strip().rstrip(".!?。！？:：;；\t\r\n").upper()


def _mentions_fail(verdict: str) -> bool:
    """Whether a normalized verdict rejects the output anywhere in its text.

    ``startswith("PASS")`` was evaluated after ``startswith("FAIL")``, so a
    reply that restates the choice before deciding — ``"PASS/FAIL
    determination: FAIL - no tests"`` — matched PASS and let a rejected output
    through the gate.  A gate must fail closed: any FAIL token anywhere means
    fail, and only a reply that leads with PASS and never says FAIL passes.
    """
    return re.search(r"\bFAIL\b", verdict) is not None


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
            # Substitute only the ``{output}`` placeholder literally so a gate
            # template containing JSON samples or other literal braces (a
            # common content-authoring mistake) cannot make str.format raise
            # and silently turn every check into a permanent gate failure.
            template = self._prompt_template.replace(
                "{output}", _bounded_gate_output(output)
            )
            with llm_purpose("gate"):
                resp = await self._llm.chat([
                    {"role": "system", "content": "You are a quality gate. Evaluate the output and respond with ONLY 'PASS' or 'FAIL: <reason>'. Be strict."},
                    {"role": "user", "content": template},
                ])
            text = resp.text.strip()
            verdict = _normalized_llm_verdict(text)
            if _mentions_fail(verdict):
                # Extract the reason from the original casing, not the
                # upper-cased verdict, so the hint handed back to the node reads
                # the way the gate wrote it.  Anchor on the *last* FAIL: a reply
                # that restates the choice first ("PASS/FAIL determination: FAIL
                # - no tests") puts the deciding token last, and anchoring on the
                # first one would hand back the preamble as the reason.
                matches = list(re.finditer(r"\bFAIL\b", text, re.IGNORECASE))
                tail = text[matches[-1].end():] if matches else ""
                # Models separate the verdict from the reason with a colon or a
                # dash about equally often; keep neither in the hint.
                reason = tail.strip(":：-—– \t\r\n") or "gate rejected the output"
                return GateResult("fail", f"LLM verification: {reason}", retry_hint=reason)
            if verdict.startswith("PASS"):
                # The heuristic pre-filter passed or was uncertain (retry);
                # the LLM has now explicitly verified the output, so the gate
                # passes regardless of the pre-filter verdict.
                return GateResult("pass", "LLM verification passed")
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
