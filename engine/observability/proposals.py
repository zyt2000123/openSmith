"""Approval-gated improvement proposals derived from run diagnoses."""

from __future__ import annotations

from dataclasses import dataclass

from .diagnosis import RunDiagnosis


@dataclass(frozen=True)
class RunImprovementProposal:
    """A suggested local change; this object never mutates runtime configuration."""

    run_id: str
    agent_id: str
    status: str
    category: str | None
    title: str
    rationale: str
    suggested_change: str | None
    approval_required: bool


class ImprovementProposer:
    """Turn a diagnosis into one conservative, approval-required proposal."""

    def propose(self, diagnosis: RunDiagnosis) -> RunImprovementProposal:
        if diagnosis.status == "healthy":
            return RunImprovementProposal(
                run_id=diagnosis.run_id,
                agent_id=diagnosis.agent_id,
                status="no_action",
                category=None,
                title="No change proposed",
                rationale=diagnosis.summary,
                suggested_change=None,
                approval_required=False,
            )
        if diagnosis.primary_category == "trace_integrity":
            return RunImprovementProposal(
                run_id=diagnosis.run_id,
                agent_id=diagnosis.agent_id,
                status="no_action",
                category=diagnosis.primary_category,
                title="Trace integrity investigation required",
                rationale=diagnosis.summary,
                suggested_change=None,
                approval_required=False,
            )
        title, change = _proposal_for(diagnosis.primary_category)
        return RunImprovementProposal(
            run_id=diagnosis.run_id,
            agent_id=diagnosis.agent_id,
            status="proposed",
            category=diagnosis.primary_category,
            title=title,
            rationale=diagnosis.summary,
            suggested_change=change,
            approval_required=True,
        )


def _proposal_for(category: str | None) -> tuple[str, str]:
    proposals = {
        "tool_timeout": (
            "Review tool timeout policy",
            "Propose a scoped timeout or retry-policy adjustment for the affected tool after validating its target.",
        ),
        "budget_exhausted": (
            "Review execution budget and plan",
            "Propose a skill or prompt adjustment that reduces repeated tool calls before changing budget limits.",
        ),
        "repeated_backtracks": (
            "Review routing and skill exit criteria",
            "Propose a routing or skill-policy patch that prevents the observed backtrack cycle.",
        ),
        "run_failed": (
            "Review failing execution dependency",
            "Propose a targeted dependency, tool, or prompt correction after inspecting the retained evidence.",
        ),
    }
    return proposals.get(category, (
        "Review run prerequisites",
        "Propose a targeted change only after the blocking condition has been validated.",
    ))
