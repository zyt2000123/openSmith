from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from engine.execution import RunStateError, RunStateStore, RunStateTransitionError
from engine.safety.approval import APPROVAL_BROKER

from ..schemas.run import RunStateOut

logger = logging.getLogger(__name__)


class RunStateService:
    """Read-only server adapter for the engine-owned run state store."""

    def __init__(self, store: RunStateStore) -> None:
        self.store = store

    async def get_run(self, agent_id: str, run_id: str) -> RunStateOut:
        store = self.store
        try:
            state = await asyncio.to_thread(store.get, run_id)
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

    async def resolve_approval(
        self,
        agent_id: str,
        run_id: str,
        approval_id: str,
        *,
        approved: bool,
    ) -> RunStateOut:
        store = self.store
        try:
            state = await asyncio.to_thread(store.get, run_id)
        except (ValueError, RunStateError) as exc:
            raise HTTPException(404, "Run not found") from exc

        if state is None or state.agent_id != agent_id:
            raise HTTPException(404, "Run not found")
        if state.approval_id != approval_id:
            raise HTTPException(409, "Approval request does not match the pending run")
        if not APPROVAL_BROKER.is_pending(run_id, approval_id):
            raise HTTPException(409, "Approval request is no longer active")

        # Resolve the in-memory broker first: it is the gate the engine's tool
        # call is actually blocked on.  Resolving the store first could leave the
        # persisted run RUNNING (with its approval cleared) while the broker entry
        # was already popped by a 300s timeout — the client would get a 409 for an
        # approval the user did grant, and the tool would never run.  Resolving
        # the broker first means a 409 never mutates the store.
        if not APPROVAL_BROKER.resolve(run_id, approval_id, approved):
            raise HTTPException(409, "Approval request is no longer active")
        try:
            resolved = await asyncio.to_thread(
                store.resolve_approval,
                run_id,
                approval_id,
                approved=approved,
            )
        except (RunStateError, RunStateTransitionError) as exc:
            # The engine has already been unblocked by broker.resolve above, so
            # the tool will run; the store write failing must not surface as a
            # rejection to the user.  The engine's own TOOL_CALL_RESULT projection
            # will re-sync the persisted state when the tool completes.
            logger.warning(
                "approval granted but run state persist failed (run=%s)", run_id,
                exc_info=True,
            )
            try:
                state = await asyncio.to_thread(store.get, run_id)
            except (ValueError, RunStateError):
                state = None
            if state is None:
                raise HTTPException(503, "Run state is temporarily unavailable")
            return RunStateOut(**state.to_dict())
        return RunStateOut(**resolved.to_dict())
