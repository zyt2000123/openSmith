import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi import Depends

from common.database import close_db
from common.config import AGENT_DIR
from engine.execution import RunStateError, RunStateStore
from engine.llm.observability import set_default_generation_sink

from .infrastructure.auth import get_local_token, require_auth
from .infrastructure.database import get_app_db
from .routers import (
    agent,
    config,
)
from .services.auto_task_service import cancel_background_runs
from .services.scheduler import run_scheduler
from .services.token_stats_service import TokenStatsService

from .services.engine_runtime import close_shared_llm_clients, load_runtime_identity_catalog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_local_token()
    await get_app_db()
    load_runtime_identity_catalog(force=True)
    try:
        recovered = RunStateStore(AGENT_DIR).recover_interrupted()
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
