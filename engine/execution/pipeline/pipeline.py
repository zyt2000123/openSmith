"""Pipeline executor — walks a SkillChain node-by-node with ReAct + gates.

Extracted from agent_loop.py so pipeline execution logic is isolated from
routing, lifecycle management, and legacy compatibility.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import uuid4

from .backtrack import FailureLoopGuard, FailureSignature
from engine.observability import EventType, ExecutionEvent, raw_text_delta
from .gate import Gate, GateResult, LLMGate, coerce_gate_result
from .pipeline_context import (
    CTX_AGENT_ID,
    CTX_IDENTITY_ID,
    CTX_RETRY_HINT,
    CTX_ROUTE_ID,
    CTX_RUBRIC_FEEDBACK,
    CTX_SESSION_ID,
    CTX_STATE_DIR,
    CTX_WORKING_DIR,
    output_key,
)
from engine.execution.react.react_loop import react_event_loop

if TYPE_CHECKING:
    from engine.llm.port import LLMPort
    from engine.safety.tool_guard import ToolGuard
    from engine.skill.registry import SkillRegistry
    from engine.tool.registry import ToolRegistry
    from .skill_chain import SkillChain

logger = logging.getLogger(__name__)

# 兜底层重试上限：base gate 不过时同节点最多重跑的次数（含首次）。
_BASE_GATE_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Public: pipeline runner
# ---------------------------------------------------------------------------


async def run_pipeline(
    chain: "SkillChain",
    llm: "LLMPort",
    user_message: str,
    base_messages: list[dict],
    tool_registry: "ToolRegistry",
    skill_registry: "SkillRegistry",
    tool_guard: "ToolGuard | None",
    guard: FailureLoopGuard,
    max_react_iters: int,
    context: dict,
    gate_llm: "LLMPort | None" = None,
    start_node_idx: int = 0,
    disabled_skill_names: frozenset[str] = frozenset(),
) -> AsyncGenerator[ExecutionEvent, None]:
    """Execute a pipeline: walk nodes sequentially, ReAct each, gate-check.

    ``start_node_idx`` > 0 resumes a crash-interrupted chain: earlier nodes'
    outputs must already be present in ``context`` (from the checkpoint).
    """
    from engine.skill.executor import execute_skill_events

    node_idx = start_node_idx
    max_backtracks = 5
    backtrack_count = 0
    committed_provisional_output: dict[str, bool] = {}

    while node_idx < len(chain.nodes):
        node = chain.nodes[node_idx]

        if node.condition is not None and not node.condition(context):
            node_idx += 1
            continue

        # A user-disabled skill is distinct from one that is simply absent
        # from the registry.  The latter keeps the historical generic-ReAct
        # fallback, while the former must not execute at all.
        if node.skill_name in disabled_skill_names:
            logger.info("skipping user-disabled pipeline skill %r", node.skill_name)
            node_idx += 1
            continue

        yield ExecutionEvent(EventType.SKILL_START, {"skill": node.skill_name, "index": node_idx})

        # 两层门禁：先过 chain.base_gates（YAML 声明的兜底层，可为空），
        # 通过后再过节点自己的 gate（领域层）。引擎不预置任何具体门禁。
        base_gates = chain.base_gates
        max_attempts = _BASE_GATE_MAX_RETRIES if base_gates else 1
        attempt = 0
        output = ""
        base_passed = False
        base_result: GateResult | None = None
        provision_id = ""
        provision_settled = True

        try:
            while attempt < max_attempts:
                provision_id = f"{node.skill_name}:{node_idx}:{attempt}:{uuid4().hex}"
                provision_settled = False
                skill = skill_registry.get(node.skill_name)

                if skill is None:
                    logger.warning(
                        "skill %r not in registry; node degrades to plain ReAct with a prompt prefix",
                        node.skill_name,
                    )
                    messages = base_messages + [{"role": "user", "content": f"[Skill: {node.skill_name}] {user_message}"}]
                    # attempt==1：base gate 重试；attempt==0 且有 hint：域门禁
                    # retry 重进本节点（见下方 retry 分支写入 CTX_RETRY_HINT）。
                    if attempt <= 1 and context.get(CTX_RETRY_HINT):
                        messages.append({"role": "user", "content": context[CTX_RETRY_HINT]})
                    elif attempt == 2:
                        messages.append({"role": "user", "content": "Switch strategy: try a completely different approach."})
                    event_stream = react_event_loop(
                        llm, messages, tool_registry, tool_guard, max_react_iters,
                        provisional_lifecycle=False,
                    )
                else:
                    skill_context = dict(context)
                    if attempt <= 1 and skill_context.get(CTX_RETRY_HINT):
                        skill_context[CTX_RUBRIC_FEEDBACK] = skill_context[CTX_RETRY_HINT]
                    elif attempt == 2:
                        skill_context[CTX_RUBRIC_FEEDBACK] = "Switch strategy: try a completely different approach."
                    messages = [{"role": "user", "content": user_message}]
                    event_stream = execute_skill_events(
                        skill, llm, tool_registry, messages, skill_context,
                        max_react_iters, tool_guard=tool_guard, provisional_lifecycle=False,
                        react_event_loop_fn=react_event_loop,
                    )

                result = _NodeResult()
                async for event in _collect_node_events(event_stream, provision_id):
                    if isinstance(event, _NodeResult):
                        result = event
                    else:
                        yield event

                if result.incomplete_reason or result.failed_reason:
                    reason = result.failed_reason or result.incomplete_reason
                    yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                        "provision_id": provision_id, "reason": reason,
                    })
                    provision_settled = True
                    # ``result.text`` was never accepted by this node's gate.
                    # It may be a content-filtered or truncated provider draft,
                    # so never turn it into a normal reply (or persisted turn).
                    _clear_checkpoint(context)
                    yield ExecutionEvent(EventType.SKILL_END, {
                        "skill": node.skill_name,
                        "status": "incomplete" if result.incomplete_reason else "error",
                    })
                    yield ExecutionEvent(EventType.DONE, {})
                    return
                output = result.text

                # 第一层：兜底门禁。为空则本次产出直接进入领域门禁。
                if not base_gates:
                    base_passed = True
                    break
                base_result = await _check_base_gates(base_gates, output, context, gate_llm or llm)
                if base_result.verdict == "pass":
                    base_passed = True
                    break
                context[CTX_RETRY_HINT] = base_result.retry_hint or ""
                yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                    "provision_id": provision_id, "reason": "rubric_retry",
                })
                provision_settled = True
                attempt += 1

            context.pop(CTX_RETRY_HINT, None)

            if not base_passed:
                _clear_checkpoint(context)
                yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "blocked"})
                yield ExecutionEvent(EventType.BLOCKED, {
                    "skill": node.skill_name,
                    "reason": base_result.reason if base_result else "base gate failed",
                })
                yield ExecutionEvent(EventType.DONE, {})
                return

            if isinstance(node.gate, LLMGate):
                node.gate.set_llm(gate_llm or llm)
            gate_result = coerce_gate_result(await node.gate.check(output, context))
            yield ExecutionEvent(EventType.GATE_RESULT, {
                "skill": node.skill_name, "verdict": gate_result.verdict, "reason": gate_result.reason,
            })

            if gate_result.verdict == "pass":
                yield ExecutionEvent(EventType.PROVISIONAL_COMMIT, {"provision_id": provision_id})
                provision_settled = True
                context[output_key(node.skill_name)] = output
                committed_provisional_output[node.skill_name] = result.was_provisional
                _save_checkpoint(context, node_idx)
                yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "passed"})
                node_idx += 1
                continue

            sig = FailureSignature(error_type=node.skill_name, context_hash=hashlib.md5(output.encode()).hexdigest()[:8])
            action = guard.record(sig)

            yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                "provision_id": provision_id, "reason": gate_result.reason,
            })
            provision_settled = True

            if action == "switch" and node.skill_name not in chain.backtrack_map:
                # 无可切换的回退策略时必须终止，不许退化成同节点无限 retry。
                action = "blocked"

            if action == "blocked":
                _clear_checkpoint(context)
                yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "blocked"})
                yield ExecutionEvent(EventType.BLOCKED, {"skill": node.skill_name, "reason": gate_result.reason})
                yield ExecutionEvent(EventType.DONE, {})
                return

            if action == "switch":
                backtrack_count += 1
                if backtrack_count > max_backtracks:
                    _clear_checkpoint(context)
                    yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "blocked"})
                    yield ExecutionEvent(EventType.BLOCKED, {"skill": node.skill_name, "reason": "max backtracks"})
                    yield ExecutionEvent(EventType.DONE, {})
                    return
                target = chain.backtrack_map[node.skill_name]
                target_idx = next((i for i, n in enumerate(chain.nodes) if n.skill_name == target), None)
                if target_idx is None:
                    # backtrack 映射指向不存在的节点是配置错误；静默跳回节点 0
                    # 会重跑已通过的步骤且与 BACKTRACK 事件宣称的目标不符。
                    logger.warning("backtrack target %r for node %r not in chain", target, node.skill_name)
                    _clear_checkpoint(context)
                    yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "blocked"})
                    yield ExecutionEvent(EventType.BLOCKED, {
                        "skill": node.skill_name, "reason": f"backtrack target {target!r} not found",
                    })
                    yield ExecutionEvent(EventType.DONE, {})
                    return
                yield ExecutionEvent(EventType.BACKTRACK, {"from": node.skill_name, "to": target})
                node_idx = target_idx
                continue

            # 域门禁产出的 retry_hint 必须随重试流回节点，否则唯一一次
            # retry 是盲跑（LLMGate 特意生成的反馈被静默丢弃）。
            if gate_result.retry_hint:
                context[CTX_RETRY_HINT] = gate_result.retry_hint
            yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "retry"})
        except Exception:
            if not provision_settled:
                yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                    "provision_id": provision_id, "reason": "execution_error",
                })
            yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "error"})
            # 进程内异常与 blocked/incomplete/failed 一样是终态：错误已上抛给
            # 调用方，重跑应从头开始。只有真正的进程崩溃（走不到这里）才留下
            # checkpoint，由 agent_loop 的 crash-resume 消费。
            _clear_checkpoint(context)
            raise

    # Provisional events now reach the session UI.  The final semantic text is
    # still emitted for persistence, but consumers that rendered the accepted
    # provisional text must not append it again.
    for node in reversed(chain.nodes):
        key = output_key(node.skill_name)
        if key in context:
            data: dict[str, object] = {"text": context[key]}
            if committed_provisional_output.get(node.skill_name):
                data["already_streamed"] = True
            yield ExecutionEvent(EventType.TEXT_DELTA, data)
            break

    _clear_checkpoint(context)
    yield ExecutionEvent(EventType.DONE, {})


# ---------------------------------------------------------------------------
# Internal: base-gate layer
# ---------------------------------------------------------------------------


async def _check_base_gates(
    gates: list["Gate"],
    output: str,
    context: dict,
    llm: "LLMPort",
) -> GateResult:
    """Run the YAML-declared base gates in order; first non-pass wins."""
    for gate in gates:
        if isinstance(gate, LLMGate):
            gate.set_llm(llm)
        result = coerce_gate_result(await gate.check(output, context))
        if result.verdict != "pass":
            return result
    return GateResult("pass", "base gates passed")


# ---------------------------------------------------------------------------
# Internal: event collection (eliminates the 3x copy-paste)
# ---------------------------------------------------------------------------


class _NodeResult:
    __slots__ = ("text", "was_provisional", "incomplete_reason", "failed_reason")

    def __init__(self) -> None:
        self.text = ""
        self.was_provisional = False
        self.incomplete_reason: str | None = None
        self.failed_reason: str | None = None


async def _collect_node_events(
    event_stream: AsyncGenerator[ExecutionEvent, None],
    provision_id: str,
) -> AsyncGenerator[ExecutionEvent | _NodeResult, None]:
    """Collect TEXT_DELTA into result, convert RAW to PROVISIONAL, forward the rest.

    Yields ExecutionEvents to forward, then a final _NodeResult with collected text.
    """
    parts: list[str] = []
    was_provisional = False
    incomplete_reason: str | None = None
    failed_reason: str | None = None

    async for event in event_stream:
        if event.type == EventType.TEXT_DELTA:
            parts.append(str(event.data.get("text", "")))
            continue
        elif event.type == EventType.RAW_RESPONSE_EVENT:
            delta = raw_text_delta(event)
            if delta is not None:
                was_provisional = True
                yield ExecutionEvent(EventType.PROVISIONAL_TEXT_DELTA, {
                    "text": delta, "provision_id": provision_id,
                })
            continue
        elif event.type == EventType.INCOMPLETE:
            incomplete_reason = str(event.data.get("reason", "agent_incomplete"))
        elif event.type == EventType.FAILED:
            failed_reason = str(event.data.get("reason", "agent_failed"))
        yield event

    result = _NodeResult()
    result.text = "".join(parts)
    result.was_provisional = was_provisional
    result.incomplete_reason = incomplete_reason
    result.failed_reason = failed_reason
    yield result


# ---------------------------------------------------------------------------
# Internal: checkpoint helpers
# ---------------------------------------------------------------------------


def _save_checkpoint(context: dict, node_idx: int) -> None:
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return
    try:
        from .checkpoint import SessionStateManager, SessionCheckpoint
        SessionStateManager(Path(state_dir)).save(SessionCheckpoint(
            agent_id=str(context.get(CTX_AGENT_ID) or ""),
            session_id=session_id,
            identity_id=str(context.get(CTX_IDENTITY_ID) or ""),
            route_id=str(context.get(CTX_ROUTE_ID) or ""),
            skill_chain_index=node_idx,
            context={k: v for k, v in context.items() if not k.startswith("_")},
            timestamp=datetime.now(timezone.utc).isoformat(),
            working_dir=str(context.get(CTX_WORKING_DIR) or ""),
        ))
    except Exception:
        logger.exception("failed to save session checkpoint")


def _clear_checkpoint(context: dict) -> None:
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return
    try:
        from .checkpoint import SessionStateManager
        SessionStateManager(Path(state_dir)).clear(session_id)
    except Exception:
        logger.exception("failed to clear session checkpoint")
