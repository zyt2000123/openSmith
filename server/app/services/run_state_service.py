from __future__ import annotations

import logging

from fastapi import HTTPException

from engine.execution.orchestration.run_state import RunStateError, RunStateStore, RunStateTransitionError
from engine.safety.approval import APPROVAL_BROKER

from ..schemas.run import RunStateOut

logger = logging.getLogger(__name__)


class RunStateService:
    """Read-only server adapter for the engine-owned run state store."""

    def __init__(self, store: RunStateStore) -> None:
        self.store = store

    def get_run(self, agent_id: str, run_id: str) -> RunStateOut:
        store = self.store
        try:
            state = store.get(run_id)
        except ValueError:
            raise HTTPException(404, "Run not found")
        except RunStateError:
            logger.warning("unable to read run state (run=%s)", run_id, exc_info=True)
            raise HTTPException(503, "Run state is temporarily unavailable")

        # The API is local-token authenticated, but still enforce the owning
        # agent boundary so a future multi-agent server cannot leak state.
        if state is None or state.agent_id != agent_id:
            raise HTTPException(404, "Run not found")
        return RunStateOut(**state.to_dict())

    def resolve_approval(
        self,
        agent_id: str,
        run_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> RunStateOut:
        store = self.store
        try:
            state = store.get(run_id)
        except (ValueError, RunStateError) as exc:
            raise HTTPException(404, "Run not found") from exc

        if state is None or state.agent_id != agent_id:
            raise HTTPException(404, "Run not found")
        if state.approval_id != approval_id:
            raise HTTPException(409, "Approval request does not match the pending run")
        if not APPROVAL_BROKER.is_pending(run_id, approval_id):
            raise HTTPException(409, "Approval request is no longer active")

        try:
            resolved = store.resolve_approval(run_id, approval_id, approved=approved)
        except (RunStateError, RunStateTransitionError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if not APPROVAL_BROKER.resolve(run_id, approval_id, approved):
            raise HTTPException(409, "Approval request is no longer active")
        return RunStateOut(**resolved.to_dict())
