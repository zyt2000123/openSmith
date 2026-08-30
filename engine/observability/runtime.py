"""Stable write-side boundary for one observed Agent run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from engine.execution.events import EventType, ExecutionEvent, RunObservationContext

from .projections import RunSummary, RunSummaryProjection
from .recorder import EventProjection, RunEventRecorder
from .summary_store import RunMetadata, RunSummaryStore
from .trace_store import TraceStore

logger = logging.getLogger(__name__)


class RunObservation:
    """Single write-side façade for traces, summaries, and projections."""

    def __init__(
        self,
        recorder: RunEventRecorder,
        *,
        identity_binder: Callable[[str], None] | None = None,
        route_binder: Callable[[str | None, str | None], None] | None = None,
    ) -> None:
        self._recorder = recorder
        self._identity_binder = identity_binder
        self._route_binder = route_binder

    @classmethod
    def start(
        cls,
        context: RunObservationContext,
        *,
        projections: tuple[EventProjection, ...] = (),
    ) -> "RunObservation":
        """Start a best-effort local observation without exposing storage."""
        try:
            trace_store: TraceStore | None = TraceStore(context.profile_dir)
        except OSError:
            logger.warning("failed to initialize run trace (run=%s)", context.run_id, exc_info=True)
            trace_store = None
        metadata = _metadata_for(context)
        try:
            summary_store: RunSummaryStore | None = RunSummaryStore(context.profile_dir)
        except OSError:
            logger.warning("failed to initialize run summary store (run=%s)", context.run_id, exc_info=True)
            summary_store = None
        summary_sinks = ()
        metadata_holder = [metadata]
        if summary_store is not None:
            summary_sinks = (
                lambda summary: summary_store.save(metadata_holder[0], summary),
            )

        def bind_identity(identity_id: str) -> None:
            metadata_holder[0] = replace(
                metadata_holder[0],
                identity_id=identity_id,
            )

        def bind_route(route_id: str | None, pipeline_id: str | None) -> None:
            metadata_holder[0] = replace(
                metadata_holder[0],
                route_id=route_id or None,
                pipeline_id=pipeline_id or None,
            )

        return cls(
            RunEventRecorder(
                context.run_id,
                trace_store=trace_store,
                projections=projections,
                summary_sinks=summary_sinks,
            ),
            identity_binder=bind_identity,
            route_binder=bind_route,
        )

    def record(self, event: ExecutionEvent) -> None:
        if event.type is EventType.ROUTE_DECIDED:
            identity_id = event.data.get("identity_id")
            if isinstance(identity_id, str) and identity_id:
                self.bind_identity(identity_id)
            route_id = event.data.get("route_id")
            pipeline_id = event.data.get("pipeline_id")
            self.bind_route(
                route_id if isinstance(route_id, str) else None,
                pipeline_id if isinstance(pipeline_id, str) else None,
            )
        self._recorder.record(event)

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        self._recorder.append_prompt_manifest(manifest)

    def bind_identity(self, identity_id: str) -> None:
        if self._identity_binder is not None:
            self._identity_binder(identity_id)

    def bind_route(self, route_id: str | None, pipeline_id: str | None) -> None:
        if self._route_binder is not None:
            self._route_binder(route_id, pipeline_id)


def finalize_interrupted_run(
    context: RunObservationContext,
    *,
    status: str,
    reason: str | None,
    finished_at: str | None = None,
) -> RunSummary:
    """Close an interrupted run without losing its already-persisted attempt.

    State recovery happens after a process dies, when no streaming lifecycle is
    alive to emit ``RUN_FINISHED``.  Rebuild only the trace tail since the last
    terminal event and merge that tail into an existing summary.  For a run
    whose trace reached its terminal event but whose summary write was
    interrupted, materialize the complete trace instead of appending a second
    terminal event.

    A failed trace verification is deliberately quarantined: it is never
    extended or used as evidence.  The synthetic terminal summary makes the
    recovered run discoverable, and the reader reports the trace-integrity
    incident separately.
    """
    terminal = ExecutionEvent(EventType.RUN_FINISHED, {
        "run_id": context.run_id,
        "status": status,
        **({"reason": reason} if reason else {}),
    })
    summary_store = _summary_store(context)
    existing = summary_store.get(context.run_id) if summary_store is not None else None

    try:
        trace_store = TraceStore(context.profile_dir)
        verification = trace_store.verify(context.run_id)
    except (OSError, ValueError):
        logger.warning(
            "failed to verify interrupted run trace (run=%s)",
            context.run_id,
            exc_info=True,
        )
        verification = None
        trace_store = None

    if trace_store is None or verification is None or not verification.ok:
        return _save_recovery_summary(
            summary_store,
            context,
            _summary_for(context.run_id, [terminal]),
            finished_at,
        )

    try:
        events = _execution_events(trace_store.read(context.run_id))
    except (OSError, ValueError):
        logger.warning(
            "failed to read interrupted run trace (run=%s)",
            context.run_id,
            exc_info=True,
        )
        return _save_recovery_summary(
            summary_store,
            context,
            _summary_for(context.run_id, [terminal]),
            finished_at,
        )

    last_terminal = max(
        (index for index, event in enumerate(events) if event.type is EventType.RUN_FINISHED),
        default=-1,
    )
    if last_terminal >= 0 and _terminal_matches(events[last_terminal], terminal):
        if existing is not None:
            return existing.summary
        return _save_recovery_summary(
            summary_store,
            context,
            _summary_for(context.run_id, events),
            finished_at,
        )

    try:
        # A resumed run legitimately extends a previously anchored trace.
        # Clear that stale anchor before appending the recovery terminal.
        trace_store.reopen(context.run_id)
        trace_store.append(context.run_id, terminal)
        trace_store.seal(context.run_id)
    except (OSError, ValueError):
        logger.warning(
            "failed to append interrupted run terminal trace (run=%s)",
            context.run_id,
            exc_info=True,
        )

    summary_events = [*events[last_terminal + 1 :], terminal]
    if existing is None:
        # No prior durable summary means the full valid trace is authoritative.
        summary_events = [*events, terminal]
    return _save_recovery_summary(
        summary_store,
        context,
        _summary_for(context.run_id, summary_events),
        finished_at,
    )


def _metadata_for(context: RunObservationContext) -> RunMetadata:
    return RunMetadata(
        run_id=context.run_id,
        agent_id=context.agent_id,
        session_id=context.session_id,
        identity_id=context.identity_id,
        working_dir=context.working_dir,
        forced_skill=context.forced_skill,
        created_at=context.created_at or datetime.now(timezone.utc).isoformat(),
    )


def _summary_store(context: RunObservationContext) -> RunSummaryStore | None:
    try:
        return RunSummaryStore(context.profile_dir)
    except OSError:
        logger.warning(
            "failed to initialize run summary store during recovery (run=%s)",
            context.run_id,
            exc_info=True,
        )
        return None


def _save_recovery_summary(
    summary_store: RunSummaryStore | None,
    context: RunObservationContext,
    summary: RunSummary,
    finished_at: str | None = None,
) -> RunSummary:
    if summary_store is None:
        return summary
    try:
        return summary_store.save(
            _metadata_for(context), summary, finished_at=finished_at
        ).summary
    except (OSError, ValueError):
        logger.warning(
            "failed to persist recovered run summary (run=%s)",
            context.run_id,
            exc_info=True,
        )
        return summary


def _execution_events(records: list[dict[str, object]]) -> list[ExecutionEvent]:
    events: list[ExecutionEvent] = []
    for record in records:
        event_type = record.get("type")
        data = record.get("data")
        if not isinstance(event_type, str) or not isinstance(data, dict):
            continue
        try:
            events.append(ExecutionEvent(EventType(event_type), dict(data)))
        except ValueError:
            # Prompt manifests are stored in the same JSONL file but are not
            # execution events and must not affect the aggregate summary.
            continue
    return events


def _summary_for(run_id: str, events: list[ExecutionEvent]) -> RunSummary:
    projection = RunSummaryProjection(run_id)
    for event in events:
        projection.record(event)
    return projection.snapshot()


def _terminal_matches(left: ExecutionEvent, right: ExecutionEvent) -> bool:
    return (
        left.data.get("status") == right.data.get("status")
        and left.data.get("reason") == right.data.get("reason")
    )
