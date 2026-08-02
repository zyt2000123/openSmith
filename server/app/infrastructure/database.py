from __future__ import annotations

import asyncio

import aiosqlite

from common.database import get_db

from .schema import ensure_schema

_initialized_db: aiosqlite.Connection | None = None
# The schema lock is (re)created for each connection lifecycle. An asyncio.Lock
# is bound to the event loop that first awaits it, so after close_db/reconnect
# (or a runtime path change) a lock held over a dead loop would raise on use.
_schema_lock: asyncio.Lock | None = None
_schema_lock_connection: aiosqlite.Connection | None = None


async def get_app_db() -> aiosqlite.Connection:
    global _initialized_db, _schema_lock, _schema_lock_connection
    db = await get_db()
    if _initialized_db is not db:
        # Double-checked locking: ensure_schema runs startup cleanup (e.g. the
        # agent_profiles de-duplication) which must not race under concurrent
        # first access. Recreate the lock when the guarded connection changed.
        if _schema_lock is None or _schema_lock_connection is not db:
            _schema_lock = asyncio.Lock()
            _schema_lock_connection = db
        async with _schema_lock:
            if _initialized_db is not db:
                await ensure_schema(db)
                _initialized_db = db
    return db
