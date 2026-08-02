from __future__ import annotations

from fastapi import HTTPException

from engine.observability import ObservabilityReader, RunSummaryRecord

from ..schemas.observability import AgentHealthOut, RunDiagnosisOut, RunImprovementProposalOut, RunIncidentOut, RunSummaryOut, RunTraceEventOut


class ObservabilityService:
    """Read-only, agent-scoped access to local run observability records."""

    def __init__(self, reader: ObservabilityReader) -> None:
        self.reader = reader

    def list_runs(self, agent_id: str, *, limit: int) -> list[RunSummaryOut]:
        return [self._summary_out(record) for record in self.reader.list_runs(agent_id, limit=limit)]

    def get_run(self, agent_id: str, run_id: str) -> RunSummaryOut:
        return self._summary_out(self._owned_record(agent_id, run_id))

    def get_trace(self, agent_id: str, run_id: str, *, limit: int) -> list[RunTraceEventOut]:
        self._owned_record(agent_id, run_id)
        try:
            records = self.reader.read_trace(run_id, limit=limit)
        except ValueError as exc:
            raise HTTPException(404, "Run not found") from exc
        return [RunTraceEventOut(**record) for record in records]

    def list_incidents(self, agent_id: str, *, limit: int) -> list[RunIncidentOut]:
        return [RunIncidentOut(**incident.__dict__) for incident in self.reader.list_incidents(agent_id, limit=limit)]

    def get_diagnosis(self, agent_id: str, run_id: str) -> RunDiagnosisOut:
        self._owned_record(agent_id, run_id)
        diagnosis = self.reader.get_diagnosis(run_id)
        if diagnosis is None:
            raise HTTPException(404, "Run not found")
        return RunDiagnosisOut(**diagnosis.__dict__)

    def get_health(self, agent_id: str, *, limit: int) -> AgentHealthOut:
        return AgentHealthOut(**self.reader.get_health(agent_id, limit=limit).__dict__)

    def get_improvement_proposal(self, agent_id: str, run_id: str) -> RunImprovementProposalOut:
        self._owned_record(agent_id, run_id)
        proposal = self.reader.get_improvement_proposal(run_id)
        if proposal is None:
            raise HTTPException(404, "Run not found")
        return RunImprovementProposalOut(**proposal.__dict__)

    def _owned_record(self, agent_id: str, run_id: str) -> RunSummaryRecord:
        try:
            record = self.reader.get_run(run_id)
        except ValueError as exc:
            raise HTTPException(404, "Run not found") from exc
        if record is None or record.metadata.agent_id != agent_id:
            raise HTTPException(404, "Run not found")
        return record

    @staticmethod
    def _summary_out(record: RunSummaryRecord) -> RunSummaryOut:
        usage = record.summary.token_usage
        return RunSummaryOut(
            run_id=record.metadata.run_id,
            agent_id=record.metadata.agent_id,
            session_id=record.metadata.session_id,
            identity_id=record.metadata.identity_id,
            route_id=record.metadata.route_id,
            pipeline_id=record.metadata.pipeline_id,
            working_dir=record.metadata.working_dir,
            forced_skill=record.metadata.forced_skill,
            created_at=record.metadata.created_at,
            finished_at=record.finished_at,
            outcome=record.summary.outcome,
            reason=record.summary.reason,
            event_count=record.summary.event_count,
            event_counts=dict(record.summary.event_counts),
            tool_call_count=record.summary.tool_call_count,
            backtrack_count=record.summary.backtrack_count,
            approval_required_count=record.summary.approval_required_count,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
        )
