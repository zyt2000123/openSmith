from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path

import aiosqlite

from . import config

_db: aiosqlite.Connection | None = None
_db_path: Path | None = None
_db_lock = asyncio.Lock()
_db_init_lock = asyncio.Lock()


async def _execute_pragma(conn: aiosqlite.Connection, statement: str) -> None:
    cursor = await conn.execute(statement)
    await cursor.close()


async def _check_connection_health(conn: aiosqlite.Connection) -> bool:
    """Return whether a cached connection can still execute a trivial query."""
    try:
        cursor = await conn.execute("SELECT 1")
        await cursor.close()
    except (sqlite3.Error, ValueError):
        return False
    return True


async def _cached_connection_for(sqlite_path: Path) -> aiosqlite.Connection | None:
    """Return a live matching cached connection, closing a stale one if needed."""
    global _db, _db_path
    db_to_close: aiosqlite.Connection | None = None

    async with _db_lock:
        if _db is None:
            return None
        if _db_path == sqlite_path and await _check_connection_health(_db):
            return _db

        db_to_close = _db
        _db = None
        _db_path = None

    assert db_to_close is not None
    with suppress(sqlite3.Error, ValueError):
        await db_to_close.close()
    return None


async def get_db() -> aiosqlite.Connection:
    global _db, _db_path
    paths = config.PATHS
    sqlite_path = paths.sqlite_path
    cached = await _cached_connection_for(sqlite_path)
    if cached is not None:
        return cached

    # Directory setup hashes and copies files.  It is synchronous by design,
    # so run it in a worker and keep the database-state lock available.
    async with _db_init_lock:
        cached = await _cached_connection_for(sqlite_path)
        if cached is not None:
            return cached

        await asyncio.to_thread(paths.ensure_base_dirs)
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
        async with _db_lock:
            _db = db
            _db_path = sqlite_path
        return db


async def close_db() -> None:
    global _db, _db_path
    async with _db_init_lock:
        async with _db_lock:
            if _db is None:
                return
            db = _db
            _db = None
            _db_path = None
        await db.close()
