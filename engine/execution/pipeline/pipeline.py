"""Pipeline executor — walks a SkillChain node-by-node with ReAct + gates.

Extracted from agent_loop.py so pipeline execution logic is isolated from
routing, lifecycle management, and legacy compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import uuid4

from .backtrack import FailureLoopGuard, FailureSignature
from engine.execution.events import EventType, ExecutionEvent, raw_text_delta
from engine.execution.evidence import evidence_hash_of
from .gate import Gate, GateResult, LLMGate, coerce_gate_result
from .pipeline_context import (
    CTX_AGENT_ID,
    CTX_IDENTITY_ID,
    CTX_RETRY_HINT,
    CTX_ROUTE_ID,
    CTX_RUBRIC_FEEDBACK,
    CTX_RUN_ID,
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

# Context key carrying the per-node provisional-streaming ledger between
# agent_loop's checkpoint restore and run_pipeline.  Lives in the private
# ("_") namespace so it is never checkpointed as part of the context dict;
# the pipeline round-trips it through SessionCheckpoint.provisional_outputs.
CTX_PROVISIONAL_OUTPUTS = "_committed_provisional_output"

# react_loop's budget-exhaustion notices all end with this fixed suffix
# (engine.execution.react.budget.budget_exhausted_message).  Matching it keeps
# those system notices visible while hiding content-filtered/truncated
# provider drafts, which must never be promoted to a reply.
_BUDGET_MESSAGE_SUFFIX = (
    "I stopped to avoid an infinite loop. "
    "Please retry with a narrower request or inspect the latest failed tool result."
)


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
    prefix_cache_key: str | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Execute a pipeline: walk nodes sequentially, ReAct each, gate-check.

    ``start_node_idx`` > 0 resumes a crash-interrupted chain: earlier nodes'
    outputs must already be present in ``context`` (from the checkpoint).
    """
    from engine.skill.executor import execute_react_fallback_events, execute_skill_events

    # P5/P11: fail loudly on ambiguous topology instead of silently corrupting
    # output context keys or looping on forward/self backtrack targets.
    chain_index = _validate_chain_topology(chain)

    node_idx = start_node_idx
    max_backtracks = 5
    backtrack_count = 0
    committed_provisional_output: dict[str, bool] = dict(
        context.get(CTX_PROVISIONAL_OUTPUTS) or {}
    )
    context.pop(CTX_PROVISIONAL_OUTPUTS, None)
    # P1: map each committed output key back to the node that produced it so a
    # backtrack can evict the superseded outputs it jumps past.  Reconstructed
    # on resume from whatever outputs the checkpoint already restored.
    committed_output_index: dict[str, int] = {
        output_key(node.skill_name): index
        for index, node in enumerate(chain.nodes)
        if output_key(node.skill_name) in context
    }
    # A fallback belongs to a node, rather than to the whole chain.  The
    # mapping survives a gate-driven retry of that node, but is discarded when
    # a backtrack re-runs an earlier portion of the chain.
    fallback_reasons: dict[int, str] = {}

    while node_idx < len(chain.nodes):
        node = chain.nodes[node_idx]

        if node.condition is not None and not node.condition(context):
            # P4: a backtrack hint addressed to this skipped node must not leak
            # into the next executed node as its rubric feedback.
            context.pop(CTX_RETRY_HINT, None)
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
                fallback_reason = fallback_reasons.get(node_idx)
                if fallback_reason is None and node.skill_name in disabled_skill_names:
                    fallback_reason = f"Skill {node.skill_name!r} is disabled for this run."
                    fallback_reasons[node_idx] = fallback_reason

                skill = None if fallback_reason else skill_registry.get(node.skill_name)
                if skill is None and fallback_reason is None:
                    fallback_reason = f"Skill {node.skill_name!r} is not installed."
                    fallback_reasons[node_idx] = fallback_reason
                    logger.warning(
                        "pipeline node %r has no installed Skill; using node-local ReAct fallback",
                        node.skill_name,
                    )

                node_tool_registry = tool_registry
                if node.allowed_tools is not None:
                    scoped_to = getattr(tool_registry, "scoped_to", None)
                    if not callable(scoped_to):
                        raise RuntimeError(
                            "pipeline node declares allowed_tools but the runtime tool registry "
                            "cannot enforce a scoped capability view"
                        )
                    node_tool_registry = scoped_to(node.allowed_tools)

                skill_context = dict(context)
                if attempt <= 1 and skill_context.get(CTX_RETRY_HINT):
                    skill_context[CTX_RUBRIC_FEEDBACK] = skill_context[CTX_RETRY_HINT]
                elif attempt == 2:
                    skill_context[CTX_RUBRIC_FEEDBACK] = "Switch strategy: try a completely different approach."
                if fallback_reason is not None:
                    event_stream = execute_react_fallback_events(
                        node.skill_name,
                        fallback_reason,
                        node.instructions,
                        llm,
                        node_tool_registry,
                        base_messages,
                        skill_context,
                        max_react_iters,
                        tool_guard=tool_guard,
                        provisional_lifecycle=False,
                        react_event_loop_fn=react_event_loop,
                        prefix_cache_key=prefix_cache_key,
                    )
                else:
                    assert skill is not None
                    # Vendored upstream skills stay source-faithful.  A node can
                    # supply only the small runtime-specific contract it needs
                    # (for example, how a one-question interview pauses in
                    # Agent-Smith) without creating a duplicate skill.
                    if node.instructions:
                        skill = replace(
                            skill,
                            content=(
                                f"{skill.content.rstrip()}\n\n"
                                "## Agent-Smith chain node contract\n\n"
                                f"{node.instructions}\n"
                            ),
                        )
                    event_stream = execute_skill_events(
                        skill, llm, node_tool_registry, base_messages, skill_context,
                        max_react_iters, tool_guard=tool_guard, provisional_lifecycle=False,
                        react_event_loop_fn=react_event_loop,
                        prefix_cache_key=prefix_cache_key,
                    )

                result = _NodeResult()
                try:
                    async for event in _collect_node_events(
                        event_stream,
                        provision_id,
                        suppress_terminal_events=fallback_reason is None,
                    ):
                        if isinstance(event, _NodeResult):
                            result = event
                        else:
                            yield event
                except Exception as exc:
                    if fallback_reason is not None:
                        raise
                    reason = (
                        f"Skill {node.skill_name!r} raised {type(exc).__name__}: {exc}"
                    )
                    logger.warning("%s; using node-local ReAct fallback", reason)
                    yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                        "provision_id": provision_id, "reason": "execution_error",
                    })
                    provision_settled = True
                    fallback_reasons[node_idx] = reason
                    continue

                if result.incomplete_reason or result.failed_reason:
                    reason = result.failed_reason or result.incomplete_reason
                    yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                        "provision_id": provision_id, "reason": reason,
                    })
                    provision_settled = True
                    if fallback_reason is None:
                        fallback_reasons[node_idx] = (
                            f"Skill {node.skill_name!r} did not complete: {reason}."
                        )
                        logger.warning(
                            "pipeline node %r did not complete (%s); using node-local ReAct fallback",
                            node.skill_name,
                            reason,
                        )
                        continue
                    # P3: react_loop's budget-exhausted TEXT_DELTA is swallowed
                    # by _collect_node_events, so the user never sees why the
                    # node stopped.  Surface engine budget notices before the
                    # terminal events; content-filtered/truncated provider
                    # drafts stay hidden (never a reply or persisted turn).
                    if result.text and _is_budget_message(result.text):
                        yield ExecutionEvent(EventType.TEXT_DELTA, {"text": result.text})
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

                inferred_question = (
                    node.infer_await_user_input_from_question
                    and _ends_with_user_question(output)
                )
                if (
                    node.await_user_input_marker
                    and (
                        node.await_user_input_marker in output
                        or inferred_question
                    )
                ):
                    # This is a successful, deliberate pause — not a failed
                    # node.  Persist the question as prior-node context and
                    # leave the index on this node so the next user response
                    # re-enters the same upstream skill.
                    visible_output = output.replace(node.await_user_input_marker, "").rstrip()
                    yield ExecutionEvent(EventType.PROVISIONAL_COMMIT, {
                        "provision_id": provision_id,
                    })
                    provision_settled = True
                    context[output_key(node.skill_name)] = visible_output
                    committed_provisional_output[output_key(node.skill_name)] = result.was_provisional
                    committed_output_index[output_key(node.skill_name)] = node_idx
                    await _save_checkpoint(
                        context,
                        node_idx,
                        awaiting_user_input=True,
                        provisional_outputs=committed_provisional_output,
                    )
                    yield ExecutionEvent(EventType.SKILL_END, {
                        "skill": node.skill_name,
                        "status": "awaiting_input",
                    })
                    data: dict[str, object] = {"text": visible_output}
                    if result.was_provisional:
                        data["already_streamed"] = True
                    yield ExecutionEvent(EventType.TEXT_DELTA, data)
                    yield ExecutionEvent(EventType.AWAITING_INPUT, {
                        "skill": node.skill_name,
                        "reason": (
                            "awaiting_user_input"
                            if node.await_user_input_marker in output
                            else "awaiting_user_question"
                        ),
                    })
                    yield ExecutionEvent(EventType.DONE, {})
                    return

                # 第一层：兜底门禁。为空则本次产出直接进入领域门禁。
                if result.evidence_hash is not None:
                    yield ExecutionEvent(EventType.GATE_EVIDENCE, {
                        "skill": node.skill_name,
                        "evidence": result.evidence,
                        "evidence_hash": result.evidence_hash,
                    })
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
                if fallback_reason is None:
                    # The dedicated Skill produced an answer but could not
                    # satisfy this node's base contract. Retry the *same*
                    # node through ReAct with the gate feedback and completed
                    # predecessor outputs, never by skipping ahead.
                    fallback_reasons[node_idx] = (
                        f"Skill {node.skill_name!r} failed the base gate: "
                        f"{base_result.reason}."
                    )
                    continue
                attempt += 1

            context.pop(CTX_RETRY_HINT, None)

            if not base_passed:
                _clear_checkpoint(context)
                yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "blocked"})
                yield ExecutionEvent(EventType.BLOCKED, {
                    "skill": node.skill_name,
                    "reason": base_result.reason if base_result else "base gate failed",
                    "evidence_hash": result.evidence_hash,
                })
                yield ExecutionEvent(EventType.DONE, {})
                return

            if isinstance(node.gate, LLMGate):
                node.gate.set_llm(gate_llm or llm)
            gate_result = coerce_gate_result(await node.gate.check(output, context))
            yield ExecutionEvent(EventType.GATE_RESULT, {
                "skill": node.skill_name,
                "verdict": gate_result.verdict,
                "reason": gate_result.reason,
                "evidence_hash": result.evidence_hash,
            })

            if gate_result.verdict == "pass":
                yield ExecutionEvent(EventType.PROVISIONAL_COMMIT, {"provision_id": provision_id})
                provision_settled = True
                fallback_reasons.pop(node_idx, None)
                key = output_key(node.skill_name)
                context[key] = output
                committed_provisional_output[key] = result.was_provisional
                committed_output_index[key] = node_idx
                await _save_checkpoint(
                    context,
                    node_idx,
                    provisional_outputs=committed_provisional_output,
                )
                yield ExecutionEvent(EventType.SKILL_END, {"skill": node.skill_name, "status": "passed"})
                node_idx += 1
                continue

            yield ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
                "provision_id": provision_id, "reason": gate_result.reason,
            })
            provision_settled = True

            if fallback_reason is None:
                # A failing node-specific gate is still a failure of this
                # Skill attempt. Let ReAct repair the same node first, with
                # the gate hint as context. Only a fallback that also fails is
                # handed to the existing bounded retry/backtrack guard.
                fallback_reasons[node_idx] = (
                    f"Skill {node.skill_name!r} failed the node gate: "
                    f"{gate_result.reason}."
                )
                if gate_result.retry_hint:
                    context[CTX_RETRY_HINT] = gate_result.retry_hint
                yield ExecutionEvent(EventType.SKILL_END, {
                    "skill": node.skill_name,
                    "status": "retry",
                })
                continue

            sig = FailureSignature(error_type=node.skill_name)
            action = guard.record(sig)

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
                target_idx = chain_index.get(target)
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
                # P1: committed outputs of nodes at/after the target are
                # superseded by the re-run.  Leaving them in context lets stale
                # first-pass results leak into the re-run skills' handoff and
                # into the final reply (which picks the last committed output).
                _evict_outputs_at_or_after(
                    context, committed_output_index, committed_provisional_output, target_idx
                )
                for fallback_idx in [index for index in fallback_reasons if index >= target_idx]:
                    del fallback_reasons[fallback_idx]
                # 回溯同样要把失败原因带到目标节点。否则 planning 重跑时
                # messages 仍是最初的原始需求，模型拿不到"为什么被打回"的任何
                # 信号，大概率复现同一份产出 —— 而 FailureLoopGuard 按 skill
                # 累计、不因回溯重置，等于整条流水线只有一次纠错机会，且这次
                # 机会因无信息传递而大概率无效。下方 retry 分支早已这么做。
                if gate_result.retry_hint:
                    context[CTX_RETRY_HINT] = gate_result.retry_hint
                yield ExecutionEvent(EventType.BACKTRACK, {
                    "from": node.skill_name,
                    "to": target,
                    "reason": gate_result.reason,
                })
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
            if committed_provisional_output.get(key):
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
    __slots__ = (
        "text",
        "was_provisional",
        "incomplete_reason",
        "failed_reason",
        "evidence",
        "evidence_hash",
    )

    def __init__(self) -> None:
        self.text = ""
        self.was_provisional = False
        self.incomplete_reason: str | None = None
        self.failed_reason: str | None = None
        # Ordered tool-result evidence for this node attempt: every real
        # execution's ``{tool, call_id, result_hash, error}``, bound into one
        # ``evidence_hash`` so the gate verdict is verifiable against the
        # exact tool outputs it was computed from.
        self.evidence: list[dict] = []
        self.evidence_hash: str | None = None


def _ends_with_user_question(output: str) -> bool:
    """Recognize a terminal user-owned question in a pause-capable node.

    Upstream skills can follow their conversational contract while omitting
    Smith's private HTML pause marker. Retrying a whole node solely to obtain
    that marker wastes a model call and asks the user the same question again.
    The fallback is intentionally narrow: a pipeline must explicitly opt a
    node into question inference, and the visible final line must be a
    question. Nodes that only support an explicit marker still run their
    quality gate when a completed answer ends with an optional question.
    """
    visible = re.sub(r"<!--[^>]*-->", "", output).strip()
    return bool(re.search(r"[?？][\s>*_`\]]*$", visible))


async def _collect_node_events(
    event_stream: AsyncGenerator[ExecutionEvent, None],
    provision_id: str,
    *,
    suppress_terminal_events: bool = False,
) -> AsyncGenerator[ExecutionEvent | _NodeResult, None]:
    """Collect a node attempt while retaining control of its terminal events.

    For a dedicated Skill attempt, FAILED/INCOMPLETE are held back so the
    pipeline can perform its same-node ReAct fallback without marking the
    encompassing run terminal. The fallback attempt itself forwards those
    events if it also fails.
    """
    parts: list[str] = []
    was_provisional = False
    incomplete_reason: str | None = None
    failed_reason: str | None = None
    evidence: list[dict] = []

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
        elif event.type == EventType.TOOL_CALL_RESULT:
            # Capture evidence for every real tool execution in this node
            # attempt.  Only executed calls carry ``result_hash``; policy
            # blocks and preflight challenges produced no output.
            result_hash = event.data.get("result_hash")
            if isinstance(result_hash, str) and result_hash:
                evidence.append({
                    "tool": str(event.data.get("tool") or event.data.get("name") or ""),
                    "call_id": str(event.data.get("id") or ""),
                    "result_hash": result_hash,
                    "error": bool(event.data.get("error")),
                })
        elif event.type == EventType.INCOMPLETE:
            incomplete_reason = str(event.data.get("reason", "agent_incomplete"))
            if suppress_terminal_events:
                continue
        elif event.type == EventType.FAILED:
            failed_reason = str(event.data.get("reason", "agent_failed"))
            if suppress_terminal_events:
                continue
        yield event

    result = _NodeResult()
    result.text = "".join(parts)
    result.was_provisional = was_provisional
    result.incomplete_reason = incomplete_reason
    result.failed_reason = failed_reason
    result.evidence = evidence
    result.evidence_hash = evidence_hash_of(evidence) if evidence else None
    yield result


# ---------------------------------------------------------------------------
# Internal: topology / stale-output helpers
# ---------------------------------------------------------------------------


def _validate_chain_topology(chain: "SkillChain") -> dict[str, int]:
    """Build the unique skill-name -> node-index map, failing loudly on misuse.

    Outputs are keyed in context by skill name (``<skill>_output``), so a
    chain with duplicate skill names silently corrupts outputs — the last
    write wins and earlier work is lost.  Backtrack targets are only useful
    when they point at an earlier node: re-running an already-passed node is
    pointless, and a self target merely delays the guard's block.
    """
    index_by_name: dict[str, int] = {}
    for index, node in enumerate(chain.nodes):
        previous = index_by_name.get(node.skill_name)
        if previous is not None:
            raise ValueError(
                f"pipeline step {node.skill_name!r} appears at node {previous} and {index}; "
                "duplicate skill names corrupt the output context key"
            )
        index_by_name[node.skill_name] = index

    for source, target in chain.backtrack_map.items():
        source_idx = index_by_name.get(source)
        if source_idx is None:
            raise ValueError(f"backtrack source {source!r} is not a pipeline step")
        target_idx = index_by_name.get(target)
        if target_idx is None:
            # Handled gracefully at switch time with an explicit BLOCKED outcome.
            continue
        if target_idx >= source_idx:
            raise ValueError(
                f"backtrack target {target!r} (node {target_idx}) must precede "
                f"source {source!r} (node {source_idx})"
            )
    return index_by_name


def _evict_outputs_at_or_after(
    context: dict,
    committed_output_index: dict[str, int],
    committed_provisional_output: dict[str, bool],
    target_idx: int,
) -> None:
    """Drop committed outputs of every node at/after ``target_idx`` (P1).

    On a backtrack the chain re-runs from the target node, so any output a
    node at/after the target produced earlier is superseded.  Evicting them
    from context (and therefore from the next checkpoint) stops stale
    first-pass results from leaking into the re-run skills' handoff or the
    final reply.
    """
    for stale_key in [
        key for key, index in committed_output_index.items() if index >= target_idx
    ]:
        context.pop(stale_key, None)
        committed_provisional_output.pop(stale_key, None)
        del committed_output_index[stale_key]


def _is_budget_message(text: str) -> bool:
    """True when the collected text is an engine budget-exhaustion notice."""
    return text.strip().endswith(_BUDGET_MESSAGE_SUFFIX)


# ---------------------------------------------------------------------------
# Internal: checkpoint helpers
# ---------------------------------------------------------------------------


async def _save_checkpoint(
    context: dict,
    node_idx: int,
    *,
    awaiting_user_input: bool = False,
    provisional_outputs: dict[str, bool] | None = None,
) -> None:
    session_id = str(context.get(CTX_SESSION_ID) or "")
    state_dir = str(context.get(CTX_STATE_DIR) or "")
    if not session_id or not state_dir:
        return
    try:
        from .checkpoint import SessionStateManager, SessionCheckpoint
        checkpoint = SessionCheckpoint(
            agent_id=str(context.get(CTX_AGENT_ID) or ""),
            session_id=session_id,
            identity_id=str(context.get(CTX_IDENTITY_ID) or ""),
            route_id=str(context.get(CTX_ROUTE_ID) or ""),
            skill_chain_index=node_idx,
            context={k: v for k, v in context.items() if not k.startswith("_")},
            timestamp=datetime.now(timezone.utc).isoformat(),
            working_dir=str(context.get(CTX_WORKING_DIR) or ""),
            run_id=str(context.get(CTX_RUN_ID) or ""),
            awaiting_user_input=awaiting_user_input,
            provisional_outputs=dict(provisional_outputs or {}),
        )
        # P9: json.dump + fsync + os.replace + chmod must not run on the event
        # loop; checkpoint writes happen after every committed node.
        await asyncio.to_thread(SessionStateManager(Path(state_dir)).save, checkpoint)
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
