"""Stable read-side boundary for local Agent observability records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common.hash_chain import ChainVerification

from .diagnosis import RunDiagnoser, RunDiagnosis
from .health import AgentHealth, HealthCalculator
from .incidents import IncidentDetector, RunIncident
from .proposals import ImprovementProposer, RunImprovementProposal
from .summary_store import RunSummaryRecord, RunSummaryStore
from .trace_store import TraceStore


class TraceIntegrityError(ValueError):
    """Raised when a caller asks to consume an unverifiable run trace."""

    def __init__(self, run_id: str, verification: ChainVerification) -> None:
        self.run_id = run_id
        self.verification = verification
        super().__init__(f"trace integrity verification failed for run {run_id!r}")


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

    def verify_trace(self, run_id: str) -> ChainVerification:
        """Verify a trace before exposing it to a derived consumer."""
        try:
            return self._traces.verify(run_id)
        except (OSError, ValueError):
            return ChainVerification(
                ok=False,
                failure="trace could not be read for verification",
            )

    def read_trace(self, run_id: str, *, limit: int = 300) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        trace, verification = self._verified_trace(run_id, record=self.get_run(run_id))
        if not verification.ok:
            raise TraceIntegrityError(run_id, verification)
        return trace[-limit:]

    def read_trace_from(
        self,
        run_id: str,
        *,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read only trace records appended after a durable byte cursor."""
        record = self.get_run(run_id)
        if record is not None:
            _, verification = self._verified_trace(run_id, record=record)
        else:
            verification = self.verify_trace(run_id)
        if not verification.ok:
            raise TraceIntegrityError(run_id, verification)
        return self._traces.read_from(run_id, offset=offset)

    def list_trace_run_ids(self) -> list[str]:
        """List trace ids without loading event records."""
        return self._traces.list_run_ids()

    def iter_traces(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Enumerate only traces that passed integrity verification."""
        traces: list[tuple[str, list[dict[str, Any]]]] = []
        for run_id in self.list_trace_run_ids():
            trace, verification = self._verified_trace(run_id)
            if verification.ok:
                traces.append((run_id, trace))
        return traces

    def list_incidents(self, agent_id: str, *, limit: int = 50) -> list[RunIncident]:
        """Return actionable incidents for the agent's most recent completed runs."""
        incidents: list[RunIncident] = []
        for record in self.list_runs(agent_id, limit=limit):
            trace, verification = self._verified_trace(record.metadata.run_id, record=record)
            if not verification.ok:
                incidents.append(_trace_integrity_incident(record, verification))
                continue
            incidents.extend(self._incidents.detect(record, trace))
        return sorted(incidents, key=lambda incident: incident.occurred_at, reverse=True)[:limit]

    def get_diagnosis(self, run_id: str) -> RunDiagnosis | None:
        """Derive a structured RCA for one completed run."""
        record = self.get_run(run_id)
        if record is None:
            return None
        trace, verification = self._verified_trace(run_id, record=record)
        if not verification.ok:
            return _trace_integrity_diagnosis(record, verification)
        return self._diagnoser.diagnose(record, trace)

    def get_health(self, agent_id: str, *, limit: int = 50) -> AgentHealth:
        """Aggregate health across the agent's latest completed runs."""
        records = self.list_runs(agent_id, limit=limit)
        traces = [
            self._verified_trace(record.metadata.run_id, record=record)[0]
            for record in records
        ]
        return self._health.calculate(
            agent_id,
            records,
            traces,
        )

    def get_improvement_proposal(self, run_id: str) -> RunImprovementProposal | None:
        """Return a non-executing, approval-required improvement proposal."""
        diagnosis = self.get_diagnosis(run_id)
        return self._proposer.propose(diagnosis) if diagnosis is not None else None

    def _verified_trace(
        self,
        run_id: str,
        *,
        record: RunSummaryRecord | None = None,
    ) -> tuple[list[dict[str, Any]], ChainVerification]:
        verification = self.verify_trace(run_id)
        if not verification.ok:
            return [], verification
        try:
            trace = self._traces.read(run_id)
        except (OSError, ValueError):
            return [], ChainVerification(
                ok=False,
                failure="trace could not be read after verification",
            )
        if record is not None and record.summary.event_count > 0 and not trace:
            return [], ChainVerification(
                ok=False,
                records=0,
                failure="trace is missing for a recorded run",
            )
        if record is not None and not _trace_matches_summary(trace, record):
            return [], ChainVerification(
                ok=False,
                records=len(trace),
                failure="trace terminal does not match the recorded summary",
            )
        return trace, verification


def _trace_integrity_incident(
    record: RunSummaryRecord,
    verification: ChainVerification,
) -> RunIncident:
    return RunIncident(
        run_id=record.metadata.run_id,
        agent_id=record.metadata.agent_id,
        severity="error",
        category="trace_integrity",
        message="Run trace failed integrity verification; trace-derived diagnostics are withheld.",
        reason="trace_integrity_failed",
        occurred_at=record.finished_at,
        evidence=_integrity_evidence(verification),
    )


def _trace_integrity_diagnosis(
    record: RunSummaryRecord,
    verification: ChainVerification,
) -> RunDiagnosis:
    evidence = [f"{key}={value}" for key, value in sorted(_integrity_evidence(verification).items())]
    return RunDiagnosis(
        run_id=record.metadata.run_id,
        agent_id=record.metadata.agent_id,
        status="needs_attention",
        failure_node="trace",
        primary_category="trace_integrity",
        summary="Run trace failed integrity verification; trace-derived diagnostics are withheld.",
        evidence=evidence,
        recommendation=(
            "Do not rely on this trace for root-cause analysis; preserve the local trace "
            "and investigate storage integrity before retrying."
        ),
    )


def _integrity_evidence(verification: ChainVerification) -> dict[str, int | str]:
    evidence: dict[str, int | str] = {
        "records_checked": max(0, verification.records),
        "anchored": str(verification.anchored).lower(),
    }
    if verification.anchor_matches is not None:
        evidence["anchor_matches"] = str(verification.anchor_matches).lower()
    if verification.failure:
        # Hash-chain failure text contains only verifier-owned structural
        # details (sequence/hash/anchor state), never an event payload.
        evidence["verification_failure"] = verification.failure[:200]
    return evidence


def _trace_matches_summary(trace: list[dict[str, Any]], record: RunSummaryRecord) -> bool:
    """Require a terminal trace fact to agree with the persisted aggregate."""
    outcome = record.summary.outcome
    if outcome is None:
        return True
    terminal_data: dict[str, Any] | None = None
    for event in reversed(trace):
        if event.get("type") == "run_finished" and isinstance(event.get("data"), dict):
            terminal_data = event["data"]
            break
    if terminal_data is None:
        return False
    return (
        terminal_data.get("status") == outcome
        and terminal_data.get("reason") == record.summary.reason
    )
