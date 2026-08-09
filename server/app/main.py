import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common import config as common_config
from common.database import close_db
from engine.execution import RunObservationContext, RunStateError, RunStateStore
from engine.llm.observability import set_default_generation_sink
from engine.observability import RunSummaryStore, finalize_interrupted_run
from engine.safety.tool_guard import close_audit_chains

from .infrastructure.auth import get_local_token, require_auth
from .infrastructure.database import get_app_db
from .routers import (
    agent,
    config,
)
from .services.auto_task_service import cancel_background_runs
from .services.engine_runtime import close_shared_llm_clients, load_runtime_identity_catalog
from .services.scheduler import run_scheduler
from .services.token_stats_service import TokenStatsService

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled"})


def _reconcile_startup_observability(
    state_store: RunStateStore,
    *,
    recovered_run_ids: list[str],
) -> None:
    """Close recovered runs and fill summaries lost between trace and summary IO."""
    list_states = getattr(state_store, "list_states", None)
    if not callable(list_states):
        # Keeps lightweight service doubles used by callers/tests compatible.
        return
    try:
        states = list_states()
        summaries = RunSummaryStore(common_config.PATHS.agent_dir)
    except (OSError, RunStateError):
        logger.warning("failed to enumerate run states for observability reconciliation", exc_info=True)
        return

    recovered = set(recovered_run_ids)
    for state in states:
        status = getattr(state.status, "value", str(state.status))
        if status not in _TERMINAL_RUN_STATUSES:
            continue
        # A recovered run needs a new terminal event even if a previous attempt
        # already had a summary.  Other terminal runs are revisited only when a
        # crash landed after the trace write but before the summary write.
        if state.run_id not in recovered and summaries.get(state.run_id) is not None:
            continue
        try:
            finalize_interrupted_run(
                RunObservationContext(
                    run_id=state.run_id,
                    agent_id=state.agent_id,
                    session_id=state.session_id,
                    identity_id=state.identity_id,
                    working_dir=state.working_dir,
                    forced_skill=state.forced_skill,
                    created_at=state.created_at,
                    profile_dir=common_config.PATHS.agent_dir,
                ),
                status=status,
                reason=state.reason,
            )
        except Exception:
            logger.warning(
                "failed to reconcile run observability during startup (run=%s)",
                state.run_id,
                exc_info=True,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_local_token()
    await get_app_db()
    load_runtime_identity_catalog(force=True)
    try:
        state_store = RunStateStore(common_config.PATHS.agent_dir)
        recovered = state_store.recover_interrupted()
        _reconcile_startup_observability(state_store, recovered_run_ids=recovered)
        if recovered:
            logger.warning("marked interrupted runs as resumable: %s", ", ".join(recovered))
    except (RunStateError, OSError):
        logger.warning("failed to recover interrupted runs during startup", exc_info=True)
    try:
        await TokenStatsService().sync_from_traces()
    except Exception:
        logger.warning("failed to sync token statistics during startup", exc_info=True)
    set_default_generation_sink(TokenStatsService().record_generation)
    scheduler_task = asyncio.create_task(run_scheduler())
    yield
    set_default_generation_sink(None)
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    # Detached auto-task runs outlive the request and the tick that started them,
    # so drain them before the LLM clients they are still using go away.
    await cancel_background_runs()
    await close_shared_llm_clients()
    # Anchor the audit chain head at the only boundary where it is well
    # defined: no run is still appending to the install-wide log.  A rollback
    # of the sealed log is then detectable on the next verification.
    close_audit_chains()
    await close_db()


app = FastAPI(title="Agent-Smith Server", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

app.include_router(agent.router, dependencies=[Depends(require_auth)])
app.include_router(config.router, dependencies=[Depends(require_auth)])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}
