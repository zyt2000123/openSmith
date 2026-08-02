from __future__ import annotations

import asyncio

import aiosqlite

from common.database import get_db

from .schema import ensure_schema

_initialized_db: aiosqlite.Connection | None = None
_schema_lock = asyncio.Lock()


async def get_app_db() -> aiosqlite.Connection:
    global _initialized_db
    db = await get_db()
    if _initialized_db is not db:
        # Double-checked locking: ensure_schema runs startup cleanup (e.g. the
        # agent_profiles de-duplication) which must not race under concurrent
        # first access.
        async with _schema_lock:
            if _initialized_db is not db:
                await ensure_schema(db)
                _initialized_db = db
    return db
