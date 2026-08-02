"""Background scheduler that ticks every 60 seconds to run due auto tasks."""

from __future__ import annotations

import asyncio
import logging

from common import config as common_config
from engine.execution import run_memory_idle_tick

from .auto_task_service import AutoTaskService
from .engine_runtime import build_memory_maintenance_services
from ..infrastructure.repositories.auto_task_repo import AutoTaskRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from ..infrastructure.repositories.session_repo import SessionRepo

log = logging.getLogger(__name__)

TICK_INTERVAL = 60  # seconds


def _build_service() -> AutoTaskService:
    return AutoTaskService(AutoTaskRepo(), AgentProfileRepo(), SessionRepo())


async def run_memory_maintenance_tick() -> bool:
    """Retry due memory maintenance even when no new conversation arrives."""
    services = build_memory_maintenance_services()
    return await run_memory_idle_tick(common_config.PATHS.agent_dir / "memory", services)


async def run_scheduler_tick() -> int:
    """Run one scheduler iteration; split out for deterministic tests."""
    count = await _build_service().tick()
    if not await run_memory_maintenance_tick():
        log.warning("Scheduler memory maintenance did not complete")
    return count


async def run_scheduler() -> None:
    """Loop forever, checking for due auto tasks every TICK_INTERVAL seconds."""
    log.info("Scheduler started (tick every %ds)", TICK_INTERVAL)
    while True:
        try:
            count = await run_scheduler_tick()
            if count:
                log.info("Scheduler tick: started %d auto task(s)", count)
        except asyncio.CancelledError:
            log.info("Scheduler cancelled")
            raise
        except Exception:
            log.exception("Scheduler tick error")
        await asyncio.sleep(TICK_INTERVAL)
