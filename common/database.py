from __future__ import annotations

import asyncio

import aiosqlite

from .config import SQLITE_PATH, ensure_dirs

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


async def _check_connection_health(conn: aiosqlite.Connection) -> bool:
    """Verify the connection is still usable."""
    try:
        await conn.execute("PRAGMA quick_check")
        return True
    except Exception:
        return False


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        # Health check: ensure the cached connection is still valid
        if await _check_connection_health(_db):
            return _db
        # Connection is bad, clear it and reconnect
        async with _db_lock:
            if _db is not None:
                try:
                    await _db.close()
                except Exception:
                    pass
                _db = None

    async with _db_lock:
        if _db is not None:
            return _db
        ensure_dirs()
        db = await aiosqlite.connect(str(SQLITE_PATH))
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            # Wait for a concurrent writer (server/CLI share this file) instead
            # of failing immediately with "database is locked".
            await db.execute("PRAGMA busy_timeout=5000")
            # Verify the new connection works
            await db.execute("PRAGMA quick_check")
        except BaseException:
            await db.close()
            raise
        _db = db
        return db


async def close_db() -> None:
    global _db
    async with _db_lock:
        if _db is None:
            return
        db = _db
        _db = None
        await db.close()
