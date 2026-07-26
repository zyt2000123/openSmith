"""Stable write-side boundary for one observed Agent run."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from engine.execution.events import ExecutionEvent, RunObservationContext
from .recorder import EventProjection, RunEventRecorder
from .summary_store import RunMetadata, RunSummaryStore
from .trace_store import TraceStore


logger = logging.getLogger(__name__)


class RunObservation:
    """Single write-side façade for traces, summaries, and projections."""

    def __init__(self, recorder: RunEventRecorder) -> None:
        self._recorder = recorder

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
        metadata = RunMetadata(
            run_id=context.run_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
            identity_id=context.identity_id,
            working_dir=context.working_dir,
            forced_skill=context.forced_skill,
            created_at=context.created_at or datetime.now(timezone.utc).isoformat(),
        )
        try:
            summary_store: RunSummaryStore | None = RunSummaryStore(context.profile_dir)
        except OSError:
            logger.warning("failed to initialize run summary store (run=%s)", context.run_id, exc_info=True)
            summary_store = None
        summary_sinks = ()
        if summary_store is not None:
            summary_sinks = (lambda summary: summary_store.save(metadata, summary),)
        return cls(RunEventRecorder(
            context.run_id,
            trace_store=trace_store,
            projections=projections,
            summary_sinks=summary_sinks,
        ))

    def record(self, event: ExecutionEvent) -> None:
        self._recorder.record(event)

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        self._recorder.append_prompt_manifest(manifest)
