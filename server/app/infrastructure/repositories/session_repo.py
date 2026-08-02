from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..database import get_app_db


class SessionRepo:

    async def list_by_agent(self, agent_id: str) -> list[dict]:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT s.id, s.agent_id, s.identity_id, s.model_profile, s.title, s.created_at, "
            "  (SELECT content FROM messages m WHERE m.session_id=s.id ORDER BY m.created_at DESC LIMIT 1) as last_message_preview, "
            "  (SELECT created_at FROM messages m WHERE m.session_id=s.id ORDER BY m.created_at DESC LIMIT 1) as last_message_at, "
            "  (SELECT count(*) FROM messages m WHERE m.session_id=s.id) as message_count "
            "FROM sessions s WHERE s.agent_id=? ORDER BY s.created_at DESC",
            (agent_id,),
        )
        result = []
        for r in rows:
            d = dict(r)
            preview = d.get("last_message_preview") or ""
            if len(preview) > 100:
                d["last_message_preview"] = preview[:100] + "..."
            result.append(d)
        return result

    async def create(
        self,
        agent_id: str,
        title: str,
        identity_id: str | None = None,
        model_profile: str | None = None,
    ) -> dict:
        db = await get_app_db()
        sid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO sessions (id, agent_id, identity_id, model_profile, title, created_at) VALUES (?,?,?,?,?,?)",
            (sid, agent_id, identity_id, model_profile, title, now),
        )
        await db.commit()
        return {
            "id": sid,
            "agent_id": agent_id,
            "identity_id": identity_id,
            "model_profile": model_profile,
            "title": title,
            "created_at": now,
        }

    async def exists(self, session_id: str, agent_id: str) -> bool:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT id FROM sessions WHERE id=? AND agent_id=?",
            (session_id, agent_id),
        )
        return bool(rows)

    async def get_owned(self, session_id: str, agent_id: str) -> dict | None:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, identity_id, model_profile, title, created_at FROM sessions WHERE id=? AND agent_id=?",
            (session_id, agent_id),
        )
        return dict(rows[0]) if rows else None

    async def claim_identity(
        self,
        session_id: str,
        agent_id: str,
        identity_id: str,
    ) -> bool:
        """Pin an identity once; a concurrent caller cannot overwrite it."""
        db = await get_app_db()
        cursor = await db.execute(
            "UPDATE sessions SET identity_id=? "
            "WHERE id=? AND agent_id=? AND identity_id IS NULL",
            (identity_id, session_id, agent_id),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def exists_by_id(self, session_id: str) -> bool:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,)
        )
        return bool(rows)

    async def delete_owned(self, session_id: str, agent_id: str) -> bool:
        """Delete a session only when it belongs to the requesting agent."""
        db = await get_app_db()
        cursor = await db.execute(
            "DELETE FROM sessions WHERE id=? AND agent_id=?",
            (session_id, agent_id),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def get_recent_messages(self, session_id: str, limit: int) -> list[dict]:
        """Fetch the last N messages in chronological order (DB-side LIMIT)."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM (SELECT * FROM messages WHERE session_id=? "
            "ORDER BY created_at DESC LIMIT ?) sub ORDER BY created_at ASC",
            (session_id, limit),
        )
        return [dict(r) for r in rows]

    async def get_context(self, session_id: str) -> dict:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT context_summary, context_summary_cutoff FROM sessions WHERE id=?",
            (session_id,),
        )
        return dict(rows[0]) if rows else {"context_summary": "", "context_summary_cutoff": 0}

    async def set_context(self, session_id: str, summary: str, cutoff: int) -> None:
        db = await get_app_db()
        await db.execute(
            "UPDATE sessions SET context_summary=?, context_summary_cutoff=? WHERE id=?",
            (summary, cutoff, session_id),
        )
        await db.commit()

    async def update_model_profile(
        self,
        session_id: str,
        agent_id: str,
        model_profile: str | None,
    ) -> dict | None:
        db = await get_app_db()
        cursor = await db.execute(
            "UPDATE sessions SET model_profile=? WHERE id=? AND agent_id=?",
            (model_profile, session_id, agent_id),
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
        return await self.get_owned(session_id, agent_id)

    async def get_messages(self, session_id: str, limit: int = 0, offset: int = 0) -> list[dict]:
        db = await get_app_db()
        # 0 historically meant "everything", which buffers a whole long session
        # into memory on every call.  Treat it as a bounded default; callers that
        # genuinely need the full transcript pass an explicit limit.
        effective_limit = limit if limit > 0 else 200
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (session_id, effective_limit, offset),
        )
        return [dict(r) for r in rows]

    async def get_message(self, session_id: str, message_id: str) -> dict | None:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? AND id=?", (session_id, message_id)
        )
        return dict(rows[0]) if rows else None

    async def get_messages_since(
        self, session_id: str, message_id: str, limit: int = 20
    ) -> list[dict]:
        """Rows strictly after ``message_id`` (by rowid), bounded and ascending."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? AND rowid > "
            "(SELECT rowid FROM messages WHERE id=?) ORDER BY created_at ASC LIMIT ?",
            (session_id, message_id, limit),
        )
        return [dict(r) for r in rows]

    async def get_messages_before(
        self, session_id: str, message_id: str, limit: int
    ) -> list[dict]:
        """Rows strictly before ``message_id`` (by rowid), bounded, ascending."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? AND rowid < "
            "(SELECT rowid FROM messages WHERE id=?) ORDER BY created_at DESC LIMIT ?",
            (session_id, message_id, limit),
        )
        rows.reverse()
        return [dict(r) for r in rows]

    async def add_message(self, session_id: str, role: str, content: str) -> dict:
        db = await get_app_db()
        mid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
            (mid, session_id, role, content, now),
        )
        await db.commit()
        return {"id": mid, "session_id": session_id, "role": role, "content": content, "created_at": now}

    async def discard_assistant_messages_after_user(
        self,
        session_id: str,
        user_message_id: str,
    ) -> int:
        """Remove only the interrupted run's replies, never later conversation turns."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT rowid FROM messages WHERE session_id=? AND id=? AND role='user'",
            (session_id, user_message_id),
        )
        if not rows:
            return 0
        cursor = await db.execute(
            "DELETE FROM messages WHERE session_id=? AND role='assistant' "
            "AND rowid > ? AND rowid < COALESCE(("
            "SELECT MIN(rowid) FROM messages WHERE session_id=? AND role='user' AND rowid > ?"
            "), 9223372036854775807)",
            (session_id, rows[0]["rowid"], session_id, rows[0]["rowid"]),
        )
        await db.commit()
        return cursor.rowcount
