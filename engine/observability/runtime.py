"""Stable write-side boundary for one observed Agent run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from engine.execution.events import ExecutionEvent, RunObservationContext

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
    ) -> None:
        self._recorder = recorder
        self._identity_binder = identity_binder

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

        return cls(
            RunEventRecorder(
                context.run_id,
                trace_store=trace_store,
                projections=projections,
                summary_sinks=summary_sinks,
            ),
            identity_binder=bind_identity,
        )

    def record(self, event: ExecutionEvent) -> None:
        self._recorder.record(event)

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        self._recorder.append_prompt_manifest(manifest)

    def bind_identity(self, identity_id: str) -> None:
        if self._identity_binder is not None:
            self._identity_binder(identity_id)
