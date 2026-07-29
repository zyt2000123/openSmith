"""Stable read-side boundary for local Agent observability records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .diagnosis import RunDiagnoser, RunDiagnosis
from .health import AgentHealth, HealthCalculator
from .incidents import IncidentDetector, RunIncident
from .proposals import ImprovementProposer, RunImprovementProposal
from .summary_store import RunSummaryRecord, RunSummaryStore
from .trace_store import TraceStore


class ObservabilityReader:
    """Read summaries and bounded trace events without exposing storage layout."""

    def __init__(self, profile_dir: Path) -> None:
        self._summaries = RunSummaryStore(profile_dir)
        self._traces = TraceStore(profile_dir)
        self._incidents = IncidentDetector()
        self._diagnoser = RunDiagnoser(self._incidents)
        self._health = HealthCalculator()
        self._proposer = ImprovementProposer()

    def list_runs(self, agent_id: str, *, limit: int = 50) -> list[RunSummaryRecord]:
        return self._summaries.list(agent_id, limit=limit)

    def get_run(self, run_id: str) -> RunSummaryRecord | None:
        return self._summaries.get(run_id)

    def read_trace(self, run_id: str, *, limit: int = 300) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        return self._traces.read(run_id, limit=limit)

    def read_trace_from(
        self,
        run_id: str,
        *,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read only trace records appended after a durable byte cursor."""
        return self._traces.read_from(run_id, offset=offset)

    def list_trace_run_ids(self) -> list[str]:
        """List trace ids without loading event records."""
        return self._traces.list_run_ids()

    def iter_traces(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Enumerate local traces for aggregate consumers."""
        return self._traces.iter_runs()

    def list_incidents(self, agent_id: str, *, limit: int = 50) -> list[RunIncident]:
        """Return actionable incidents for the agent's most recent completed runs."""
        incidents: list[RunIncident] = []
        for record in self.list_runs(agent_id, limit=limit):
            incidents.extend(self._incidents.detect(record, self._traces.read(record.metadata.run_id)))
        return sorted(incidents, key=lambda incident: incident.occurred_at, reverse=True)[:limit]

    def get_diagnosis(self, run_id: str) -> RunDiagnosis | None:
        """Derive a structured RCA for one completed run."""
        record = self.get_run(run_id)
        if record is None:
            return None
        return self._diagnoser.diagnose(record, self._traces.read(run_id))

    def get_health(self, agent_id: str, *, limit: int = 50) -> AgentHealth:
        """Aggregate health across the agent's latest completed runs."""
        records = self.list_runs(agent_id, limit=limit)
        return self._health.calculate(
            agent_id,
            records,
            (self._traces.read(record.metadata.run_id) for record in records),
        )

    def get_improvement_proposal(self, run_id: str) -> RunImprovementProposal | None:
        """Return a non-executing, approval-required improvement proposal."""
        diagnosis = self.get_diagnosis(run_id)
        return self._proposer.propose(diagnosis) if diagnosis is not None else None
