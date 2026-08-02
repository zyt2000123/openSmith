"""One recording boundary for an Agent run's observability data."""

from __future__ import annotations

import logging
from collections.abc import Callable

from engine.execution.events import EventType, ExecutionEvent
from .projections import RunSummary, RunSummaryProjection
from .trace_store import TraceStore


logger = logging.getLogger(__name__)
EventProjection = Callable[[ExecutionEvent], None]
SummarySink = Callable[[RunSummary], None]


class RunEventRecorder:
    """Record a run event once, then fan it out to durable and derived views.

    Trace persistence is deliberately best-effort: an unavailable local trace
    must never turn an otherwise valid agent run into a failed execution.
    Runtime-specific projections (such as ``RunStateStore``) are injected so
    this observability package remains independent of execution control.
    """

    def __init__(
        self,
        run_id: str,
        *,
        trace_store: TraceStore | None = None,
        projections: tuple[EventProjection, ...] = (),
        summary_sinks: tuple[SummarySink, ...] = (),
    ) -> None:
        self.run_id = run_id
        self._trace_store = trace_store
        self._projections = projections
        self._summary_sinks = summary_sinks
        self._summary = RunSummaryProjection(run_id)

    def record(self, event: ExecutionEvent) -> None:
        """Persist and project an event without leaking recorder failures."""
        if self._trace_store is not None:
            try:
                self._trace_store.append(self.run_id, event)
            except (OSError, ValueError):
                logger.warning(
                    "failed to append run trace (run=%s, event=%s)",
                    self.run_id,
                    event.type.value,
                    exc_info=True,
                )
        self._summary.record(event)
        for projection in self._projections:
            try:
                projection(event)
            except Exception:
                logger.warning(
                    "failed to project run event (run=%s, event=%s)",
                    self.run_id,
                    event.type.value,
                    exc_info=True,
                )
        if event.type is EventType.RUN_FINISHED:
            if self._trace_store is not None:
                try:
                    # Anchor this run's trace chain so a later rollback or edit
                    # of a finished run is detectable by a compliance reader.
                    self._trace_store.seal(self.run_id)
                except Exception:
                    logger.warning(
                        "failed to seal run trace (run=%s)",
                        self.run_id,
                        exc_info=True,
                    )
            summary = self._summary.snapshot()
            for sink in self._summary_sinks:
                try:
                    sink(summary)
                except Exception:
                    logger.warning(
                        "failed to persist run summary (run=%s)",
                        self.run_id,
                        exc_info=True,
                    )

    def append_prompt_manifest(self, manifest: dict[str, object]) -> None:
        """Persist prompt provenance through the same observability boundary."""
        if self._trace_store is None:
            return
        try:
            self._trace_store.append_prompt_manifest(self.run_id, manifest)
        except (OSError, ValueError):
            logger.warning("failed to persist prompt manifest (run=%s)", self.run_id, exc_info=True)

    def summary(self) -> RunSummary:
        """Return the current derived summary for this run."""
        return self._summary.snapshot()
