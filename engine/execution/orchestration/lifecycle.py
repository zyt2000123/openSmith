"""Lifecycle, persistence, cleanup, and public run entry points.

This is the top-level orchestrator. It does NOT execute pipelines or run
ReAct loops directly; it delegates to pipeline.py and react_loop.py.

Core dispatch and runtime preparation live in sibling modules.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator
from uuid import uuid4

from engine.llm.observability import generation_context, llm_purpose
from engine.llm.contracts import LLMResponseError
from engine.execution.events import (
    EventType,
    ExecutionEvent,
    RunEventObserver,
    RunObservationContext,
    raw_text_delta,
)
from engine.safety.fact_gate import FactGate, FactGateContext, use_fact_gate
from engine.execution.pipeline.backtrack import FailureLoopGuard
from engine.execution.react.react_loop import IncompleteAgentRunError
from .run_state import RunStateError, RunStateStore, RunStatus, project_execution_event
from .run_stream import AgentRunStream
from .runtime import EngineRequest, EngineResult, RuntimeContext, RuntimeServices
from engine.tool.ledger import ToolExecutionLedger
from engine.safety.approval import APPROVAL_BROKER, use_approval_context
from .agent_loop import run_agent_stream
from .preparation import (
    merge_request_context as _merge_context,
    prepare_runtime,
    runtime_execution_context as _runtime_execution_context,
)

__all__ = (
    "run_stream_with_runtime",
    "resume_stream_with_runtime",
    "reply_with_runtime",
    "reply_events_with_runtime",
    "reply_stream_with_runtime",
    "run_memory_idle_tick",
    "run_memory_daily_tick",
)

logger = logging.getLogger(__name__)
_RUNTIME_LEARNING_TIMEOUT_SECONDS = 30.0
_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Lifecycle: persistence + cleanup + terminal state
# ---------------------------------------------------------------------------


def _ensure_memory_lifecycle_hooks(services: RuntimeServices) -> None:
    maintenance_llm = services.background_llm or services.llm
    if services.hooks is None:
        from engine.execution.hooks import HookManager

        services.hooks = HookManager()
    hook_key = (
        id(maintenance_llm),
        id(services.gate_llm),
        services.owns_llm_clients,
        id(services.hooks),
    )
    if (
        services._memory_lifecycle_hook is not None
        and services._memory_lifecycle_hook_key == hook_key
        and services.hooks.is_registered(services._memory_lifecycle_hook)
    ):
        return
    from engine.memory.maintenance import (
        MemoryLifecycleHooks,
        MemoryMaintenanceService,
    )
    if services._memory_lifecycle_hook is not None:
        services.hooks.unregister(services._memory_lifecycle_hook)

    hook = MemoryLifecycleHooks(
        MemoryMaintenanceService(
            maintenance_llm,
            reviewer=services.gate_llm,
            defer_maintenance=not services.owns_llm_clients,
        )
    )
    services.hooks.register(hook)
    services._memory_lifecycle_hook = hook
    services._memory_lifecycle_hook_key = hook_key


async def run_memory_idle_tick(memory_dir: Path, services: RuntimeServices) -> bool:
    """Dispatch idle memory maintenance through lifecycle hooks."""
    return await _dispatch_memory_maintenance_tick(
        "memory_idle_tick",
        memory_dir,
        services,
    )


async def run_memory_daily_tick(memory_dir: Path, services: RuntimeServices) -> bool:
    """Dispatch daily memory maintenance through lifecycle hooks."""
    return await _dispatch_memory_maintenance_tick(
        "memory_daily_tick",
        memory_dir,
        services,
    )


async def _dispatch_memory_maintenance_tick(
    hook_name: str,
    memory_dir: Path,
    services: RuntimeServices,
) -> bool:
    _ensure_memory_lifecycle_hooks(services)
    try:
        from engine.execution.hooks import HookType

        results = await services.hooks.apply(
            hook_name,
            HookType.PARALLEL,
            args=(memory_dir,),
            include_failures=True,
        )
        return all(result is not False for result in results)
    except Exception:
        logger.warning("failed to dispatch %s", hook_name, exc_info=True)
        return False


async def _persist_runtime_learning(
    state_dir: Path,
    user_message: str,
    reply_text: str,
    had_tools: bool,
    services: RuntimeServices,
    *,
    terminal_status: str = "completed",
    terminal_reason: str | None = None,
) -> bool:
    """Persist memory and preferences. Returns False if any write failed."""
    ok = True
    learning_signals: list[str] = []
    learner = None
    try:
        from engine.memory.user_learner import UserPreferenceLearner
        learner = UserPreferenceLearner(state_dir)
        learning_signals = await learner.observe(user_message, reply_text)
    except Exception:
        ok = False
        logger.warning("failed to extract user-preference signals", exc_info=True)

    _ensure_memory_lifecycle_hooks(services)
    try:
        from engine.execution.hooks import HookType

        hook_name = {
            "completed": "memory_after_turn_completed",
            "incomplete": "memory_after_turn_incomplete",
            "failed": "memory_after_turn_failed",
        }.get(terminal_status, "memory_after_turn_failed")
        hook_args = (state_dir, user_message, reply_text, had_tools, learning_signals)
        if terminal_status != "completed":
            hook_args += (terminal_reason,)
        results = await services.hooks.apply(
            hook_name,
            HookType.PARALLEL,
            args=hook_args,
            include_failures=True,
        )
        ok = ok and all(result is not False for result in results)
    except Exception:
        ok = False
        logger.warning("failed to persist conversation memory", exc_info=True)
    if ok and learner is not None and learning_signals:
        try:
            learner.acknowledge(learning_signals)
        except Exception:
            ok = False
            logger.warning("failed to acknowledge user-preference signals", exc_info=True)
    return ok


def _has_successful_tool_evidence(event: ExecutionEvent) -> bool:
    """Return whether an event carries real, successful tool evidence.

    Tool starts only describe a model proposal.  Preflight challenges, policy
    blocks, and provider/tool failures never produced project evidence and
    must not make the memory pipeline label the turn as ``tool_result``.
    """
    return (
        event.type is EventType.TOOL_CALL_RESULT
        and not bool(event.data.get("blocked"))
        and not bool(event.data.get("preflight"))
        and not bool(event.data.get("error"))
    )


def _execution_error_details(
    error: BaseException,
    *,
    stage: str,
    llm: object,
) -> dict[str, object]:
    """Return a bounded classification without persisting exception messages."""
    details: dict[str, object] = {
        "kind": "internal",
        "stage": stage,
        "type": type(error).__name__[:100],
    }
    if not isinstance(error, LLMResponseError):
        return details

    message = str(error)
    status_match = _HTTP_STATUS_RE.search(message)
    if status_match is not None:
        status = int(status_match.group(1))
        details["kind"] = "provider_http"
        details["http_status"] = status
        details["retryable"] = status in {408, 429} or status >= 500
    elif any(
        marker in message.casefold()
        for marker in ("connection", "timeout", "timed out", "request failed after")
    ):
        details["kind"] = "provider_transport"
        details["retryable"] = True
    else:
        details["kind"] = "provider_protocol"

    provider = getattr(llm, "provider", None)
    if isinstance(provider, str):
        safe_provider = " ".join(provider.split())[:100]
        if safe_provider:
            details["provider"] = safe_provider
    return details


def _fact_gate_for_request(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices | None = None,
) -> FactGate:
    definitions = services.tool_registry.definitions() if services is not None else None
    return FactGate(FactGateContext(
        session_id=runtime.session_id or "",
        turn_id=uuid4().hex,
    ), tool_registry=definitions)


@dataclass(frozen=True)
class _RunEventBoundary:
    """Fan events into execution state and an optional observer Adapter."""

    state_store: RunStateStore | None
    run_id: str
    observer: RunEventObserver | None = None

    def record(self, event: ExecutionEvent) -> None:
        project_execution_event(self.state_store, self.run_id, event)
        if self.observer is not None:
            try:
                self.observer.record(event)
            except Exception:
                logger.warning(
                    "run observer rejected event (run=%s, event=%s)",
                    self.run_id,
                    event.type.value,
                    exc_info=True,
                )

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        if self.observer is not None:
            try:
                self.observer.append_prompt_manifest(manifest)
            except Exception:
                logger.warning(
                    "run observer rejected prompt manifest (run=%s)",
                    self.run_id,
                    exc_info=True,
                )


def _start_event_boundary(
    services: RuntimeServices,
    runtime: RuntimeContext,
    request: EngineRequest,
    run_id: str,
    state_store: RunStateStore | None,
) -> _RunEventBoundary:
    """Create the execution-owned event boundary for one run."""
    context = RunObservationContext(
        run_id=run_id,
        agent_id=runtime.agent_id,
        session_id=runtime.session_id,
        identity_id=request.identity_id,
        working_dir=request.working_dir,
        forced_skill=request.forced_skill,
        profile_dir=runtime.profile_dir,
    )
    observer: RunEventObserver | None = None
    if services.observation_factory is not None:
        try:
            observer = services.observation_factory(context)
        except Exception:
            logger.warning(
                "failed to initialize run observer (run=%s)",
                run_id,
                exc_info=True,
            )
    return _RunEventBoundary(state_store, run_id, observer)


def run_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AgentRunStream:
    """Create a typed, single-consumer stream for one Agent run."""
    run_id = uuid4().hex
    state_store: RunStateStore | None = None
    try:
        state_store = RunStateStore(runtime.profile_dir)
        state_store.create(
            run_id,
            agent_id=runtime.agent_id,
            session_id=runtime.session_id,
            message_id=request.message_id,
            identity_id=request.identity_id,
            working_dir=request.working_dir,
            forced_skill=request.forced_skill,
        )
    except (RunStateError, OSError, ValueError):
        logger.warning("failed to initialize run state (run=%s)", run_id, exc_info=True)
        state_store = None
    event_boundary = _start_event_boundary(
        services,
        runtime,
        request,
        run_id,
        state_store,
    )
    try:
        ledger: ToolExecutionLedger | None = ToolExecutionLedger(runtime.profile_dir, run_id)
    except Exception:
        logger.warning("failed to initialize tool execution ledger (run=%s)", run_id, exc_info=True)
        if state_store is not None:
            try:
                state_store.transition(
                    run_id,
                    RunStatus.FAILED,
                    event_type="run_setup_failed",
                    reason="tool_ledger_unavailable",
                    error="tool_ledger_unavailable",
                )
            except (RunStateError, OSError, ValueError):
                logger.warning(
                    "failed to mark tool ledger setup failure (run=%s)",
                    run_id,
                    exc_info=True,
                )
        return _failed_setup_stream(
            run_id,
            services,
            "tool_ledger_unavailable",
            event_boundary,
        )
    return AgentRunStream(
        run_id,
        _run_events_with_runtime(
            request,
            runtime,
            services,
            run_id,
            state_store,
            event_boundary,
            ledger,
        ),
        on_unstarted_close=lambda: _cancel_unstarted_run(
            run_id,
            event_boundary,
            services,
        ),
    )


async def _cancel_unstarted_run(
    run_id: str,
    event_boundary: _RunEventBoundary,
    services: RuntimeServices,
) -> None:
    """Clean up a run whose consumer closes the stream before its first event."""
    cancelled_event = ExecutionEvent(EventType.RUN_FINISHED, {
        "run_id": run_id,
        "status": RunStatus.CANCELLED.value,
        "reason": "consumer_disconnected",
    })
    event_boundary.record(cancelled_event)
    try:
        await services.close()
    except Exception:
        logger.warning("failed to close engine runtime services", exc_info=True)
    APPROVAL_BROKER.cancel_run(run_id)


def _failed_setup_stream(
    run_id: str,
    services: RuntimeServices,
    reason: str,
    event_boundary: _RunEventBoundary | None = None,
) -> AgentRunStream:
    """Expose setup failures through the same terminal stream contract."""
    boundary = event_boundary or _RunEventBoundary(None, run_id)

    async def close_unstarted() -> None:
        try:
            await services.close()
        except Exception:
            logger.warning("failed to close services after setup failure", exc_info=True)

    async def events() -> AsyncGenerator[ExecutionEvent, None]:
        try:
            for event in (
                ExecutionEvent(EventType.RUN_STARTED, {"run_id": run_id}),
                ExecutionEvent(EventType.FAILED, {"reason": reason}),
                ExecutionEvent(EventType.DONE, {}),
                ExecutionEvent(
                    EventType.RUN_FINISHED,
                    {"run_id": run_id, "status": "failed", "reason": reason},
                ),
            ):
                boundary.record(event)
                yield event
        finally:
            try:
                await services.close()
            except Exception:
                logger.warning("failed to close services after setup failure", exc_info=True)

    return AgentRunStream(run_id, events(), on_unstarted_close=close_unstarted)


def resume_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
    run_id: str,
) -> AgentRunStream:
    """Resume a recoverable run using its persisted state and tool ledger.

    The caller must provide the same session history in ``request.history``.
    Completed side-effecting calls are replayed by the run's ledger; calls
    whose prior side effect is uncertain remain blocked until an operator
    resolves them.
    """
    try:
        state_store = RunStateStore(runtime.profile_dir)
        ledger = ToolExecutionLedger(runtime.profile_dir, run_id, replay_existing=True)
        state_store.resume(run_id)
    except Exception:
        logger.warning("failed to resume run (run=%s)", run_id, exc_info=True)
        return _failed_setup_stream(
            run_id,
            services,
            "resume_setup_failed",
            _start_event_boundary(services, runtime, request, run_id, None),
        )
    event_boundary = _start_event_boundary(
        services,
        runtime,
        request,
        run_id,
        state_store,
    )
    return AgentRunStream(
        run_id,
        _run_events_with_runtime(
            request,
            runtime,
            services,
            run_id,
            state_store,
            event_boundary,
            ledger,
        ),
        on_unstarted_close=lambda: _cancel_unstarted_run(
            run_id,
            event_boundary,
            services,
        ),
    )


async def _run_events_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
    run_id: str,
    state_store: RunStateStore | None = None,
    event_boundary: _RunEventBoundary | None = None,
    ledger: ToolExecutionLedger | None = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Produce one complete run, including persistence and cleanup."""
    boundary = event_boundary or _RunEventBoundary(state_store, run_id)
    full_text: list[str] = []
    had_tools = False
    terminal_status = "completed"
    terminal_reason: str | None = None
    terminal_error: dict[str, object] | None = None
    drained = False
    state_dir: Path | None = None
    memory_persist_failed = False
    execution_stage = "runtime_prepare"

    if ledger is not None:
        services.tool_registry.bind_execution_ledger(ledger)

    try:
        run_started = ExecutionEvent(
            EventType.RUN_STARTED,
            {
                "run_id": run_id,
                "project_path": request.working_dir or "",
            },
        )
        boundary.record(run_started)
        yield run_started
        s = await prepare_runtime(request, runtime, services)
        execution_stage = "agent_execution"
        state_dir = s.state_dir
        if hasattr(s, "prompt_manifest"):
            boundary.append_prompt_manifest(s.prompt_manifest)
        guard = FailureLoopGuard()
        with use_fact_gate(_fact_gate_for_request(request, runtime, services)), use_approval_context(
            APPROVAL_BROKER, run_id
        ), generation_context(run_id=run_id, session_id=runtime.session_id), llm_purpose("main"):
            async for event in run_agent_stream(
                services.llm, s.system_prompt,
                _merge_context(request.message, request.context),
                services.tool_registry, services.skill_registry,
                s.route, s.chain, guard,
                tool_guard=services.tool_guard,
                history=request.history,
                forced_skill=request.forced_skill,
                execution_context=_runtime_execution_context(
                    runtime, s.identity, s.state_dir, s.working_dir, run_id,
                ),
                gate_llm=services.gate_llm,
                disabled_skill_names=getattr(s, "disabled_skill_names", frozenset()),
                prefix_cache_key=getattr(s, "prefix_cache_key", None),
            ):
                if event.type == EventType.TEXT_DELTA:
                    full_text.append(str(event.data.get("text", "")))
                elif event.type == EventType.INCOMPLETE:
                    terminal_status = "incomplete"
                    terminal_reason = str(event.data.get("reason", "agent_incomplete"))
                elif event.type == EventType.FAILED:
                    terminal_status = "failed"
                    terminal_reason = str(event.data.get("reason", "agent_failed"))
                    event_error = event.data.get("error")
                    if isinstance(event_error, dict):
                        terminal_error = dict(event_error)
                elif event.type == EventType.BLOCKED and terminal_status == "completed":
                    terminal_status = "incomplete"
                    terminal_reason = "blocked"
                elif _has_successful_tool_evidence(event):
                    had_tools = True
                boundary.record(event)
                yield event
        drained = True
    except Exception as exc:
        logger.exception("agent execution failed (agent=%s)", runtime.agent_id)
        terminal_status = "failed"
        terminal_reason = "execution_error"
        terminal_error = _execution_error_details(
            exc,
            stage=execution_stage,
            llm=services.llm,
        )
        failure_text = ExecutionEvent(EventType.TEXT_DELTA, {
            "text": f"⚠️ 执行失败：{type(exc).__name__}（详情见服务端日志）",
        })
        boundary.record(failure_text)
        yield failure_text
        failure_event = ExecutionEvent(EventType.FAILED, {
            "reason": terminal_reason,
            "error": terminal_error,
        })
        boundary.record(failure_event)
        yield failure_event
        done_event = ExecutionEvent(EventType.DONE, {})
        boundary.record(done_event)
        yield done_event
        drained = True
    finally:
        if (
            drained
            and state_dir is not None
            and terminal_status in {"completed", "incomplete", "failed"}
        ):
            try:
                memory_persist_failed = not await asyncio.wait_for(
                    _persist_runtime_learning(
                        state_dir, request.message, "".join(full_text), had_tools, services,
                        terminal_status=terminal_status,
                        terminal_reason=terminal_reason,
                    ),
                    timeout=_RUNTIME_LEARNING_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                memory_persist_failed = True
                logger.warning(
                    "runtime learning finalization timed out after %.1fs (run=%s)",
                    _RUNTIME_LEARNING_TIMEOUT_SECONDS,
                    run_id,
                )
            except Exception:
                memory_persist_failed = True
                logger.warning("failed to finalize conversation memory", exc_info=True)
        if not drained:
            cancelled_event = ExecutionEvent(EventType.RUN_FINISHED, {
                "run_id": run_id,
                "status": RunStatus.CANCELLED.value,
                "reason": "consumer_disconnected",
            })
            boundary.record(cancelled_event)
        try:
            await services.close()
        except Exception:
            logger.warning("failed to close engine runtime services", exc_info=True)
        if ledger is not None:
            services.tool_registry.bind_execution_ledger(None)
        APPROVAL_BROKER.cancel_run(run_id)

    if drained:
        terminal_data: dict[str, object] = {"run_id": run_id, "status": terminal_status}
        if terminal_reason:
            terminal_data["reason"] = terminal_reason
        if terminal_error:
            terminal_data["error"] = terminal_error
        if memory_persist_failed:
            # 记忆写入失败对用户默认不可见；在终态事件上打标，
            # 让前端有机会提示"本轮未写入长期记忆"。
            terminal_data["memory_persist_failed"] = True
        finished_event = ExecutionEvent(EventType.RUN_FINISHED, terminal_data)
        boundary.record(finished_event)
        yield finished_event


# ---------------------------------------------------------------------------
# Entry points (non-streaming + compatibility)
# ---------------------------------------------------------------------------


async def reply_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> EngineResult:
    """Run one engine request using the same complete stream lifecycle as SSE."""
    full_text: list[str] = []
    had_tools = False

    stream = run_stream_with_runtime(request, runtime, services)
    events = stream.stream_events()
    try:
        async for event in events:
            if event.type == EventType.TEXT_DELTA:
                full_text.append(str(event.data.get("text", "")))
            elif _has_successful_tool_evidence(event):
                had_tools = True
    finally:
        await events.aclose()

    if not stream.is_complete:
        raise RuntimeError("Agent run ended before a terminal state was emitted.")
    if stream.status == "failed":
        raise RuntimeError(stream.reason or "agent_failed")
    if stream.status == "incomplete":
        raise IncompleteAgentRunError(stream.reason or "agent_incomplete")

    return EngineResult(text="".join(full_text), had_tools=had_tools)


async def reply_events_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AsyncGenerator[ExecutionEvent, None]:
    """Compatibility adapter over run_stream_with_runtime."""
    stream = run_stream_with_runtime(request, runtime, services)
    events = stream.stream_events()
    try:
        async for event in events:
            yield event
    finally:
        await events.aclose()


async def reply_stream_with_runtime(
    request: EngineRequest,
    runtime: RuntimeContext,
    services: RuntimeServices,
) -> AsyncGenerator[str, None]:
    """Text-only stream adapter."""
    saw_raw_text = False
    async for event in reply_events_with_runtime(request, runtime, services):
        text = raw_text_delta(event, include_provisional=False)
        if text is not None:
            saw_raw_text = True
            yield text
        elif event.type == EventType.TEXT_DELTA:
            if not event.data.get("already_streamed") or not saw_raw_text:
                yield event.data.get("text", "")
        elif event.type == EventType.SKILL_START:
            yield f"\n[⚙ {event.data.get('skill', '')}]\n"
        elif event.type == EventType.GATE_RESULT:
            yield f"[门禁: {event.data.get('verdict', '')}] "
        elif event.type == EventType.BACKTRACK:
            yield f"\n[↩ 回退: {event.data.get('from', '')} → {event.data.get('to', '')}]\n"
        elif event.type == EventType.BLOCKED:
            yield f"\n[⛔ 阻断: {event.data.get('reason', '')}]\n"
