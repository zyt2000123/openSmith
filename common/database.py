from __future__ import annotations

import asyncio

import aiosqlite

from . import config

_db: aiosqlite.Connection | None = None
_db_path = None
_db_lock = asyncio.Lock()


async def _execute_pragma(conn: aiosqlite.Connection, statement: str) -> None:
    cursor = await conn.execute(statement)
    await cursor.close()


async def get_db() -> aiosqlite.Connection:
    global _db, _db_path
    async with _db_lock:
        paths = config.PATHS
        sqlite_path = paths.sqlite_path
        if _db is not None and _db_path == sqlite_path:
            return _db
        if _db is not None:
            db_to_close = _db
            _db = None
            _db_path = None
            await db_to_close.close()

        paths.ensure_base_dirs()
        db = await aiosqlite.connect(str(sqlite_path))
        try:
            db.row_factory = aiosqlite.Row
            await _execute_pragma(db, "PRAGMA journal_mode=WAL")
            await _execute_pragma(db, "PRAGMA foreign_keys=ON")
            # Wait for a concurrent writer (server/CLI share this file) instead
            # of failing immediately with "database is locked".
            await _execute_pragma(db, "PRAGMA busy_timeout=5000")
        except BaseException:
            await db.close()
            raise
        _db = db
        _db_path = sqlite_path
        return db


async def close_db() -> None:
    global _db, _db_path
    async with _db_lock:
        if _db is None:
            return
        db = _db
        _db = None
        _db_path = None
        await db.close()
