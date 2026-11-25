from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ..database import get_app_db


class SessionRepo:

    async def list_by_agent(self, agent_id: str, *, include_side: bool = False) -> list[dict]:
        db = await get_app_db()
        kind_filter = "" if include_side else " AND s.session_kind='main'"
        rows = await db.execute_fetchall(
            "SELECT s.id, s.agent_id, s.identity_id, s.model_profile, s.title, s.created_at, "
            "  s.session_kind, s.parent_session_id, s.snapshot_cutoff_message_id, "
            "  (SELECT content FROM messages m WHERE m.session_id=s.id ORDER BY m.created_at DESC LIMIT 1) as last_message_preview, "
            "  (SELECT created_at FROM messages m WHERE m.session_id=s.id ORDER BY m.created_at DESC LIMIT 1) as last_message_at, "
            "  (SELECT count(*) FROM messages m WHERE m.session_id=s.id) as message_count "
            f"FROM sessions s WHERE s.agent_id=?{kind_filter} ORDER BY s.created_at DESC",
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
            "INSERT INTO sessions (id, agent_id, identity_id, model_profile, title, created_at, session_kind) "
            "VALUES (?,?,?,?,?,?,'main')",
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
            "session_kind": "main",
            "parent_session_id": None,
            "snapshot_cutoff_message_id": None,
        }

    async def get_active_side(self, agent_id: str, parent_session_id: str) -> dict | None:
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT id, agent_id, identity_id, model_profile, title, created_at, "
            "  session_kind, parent_session_id, snapshot_cutoff_message_id, "
            "  (SELECT count(*) FROM messages m WHERE m.session_id=sessions.id) as message_count "
            "FROM sessions WHERE agent_id=? AND parent_session_id=? AND session_kind='side' "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, parent_session_id),
        )
        return dict(rows[0]) if rows else None

    async def create_side(
        self,
        agent_id: str,
        *,
        parent_session_id: str | None,
        snapshot_cutoff_message_id: str | None,
        title: str = "Side chat",
        identity_id: str | None = None,
        model_profile: str | None = None,
    ) -> dict:
        db = await get_app_db()
        sid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO sessions "
            "(id, agent_id, identity_id, model_profile, title, created_at, session_kind, "
            "parent_session_id, snapshot_cutoff_message_id) "
            "VALUES (?,?,?,?,?,?,'side',?,?)",
            (
                sid,
                agent_id,
                identity_id,
                model_profile,
                title,
                now,
                parent_session_id,
                snapshot_cutoff_message_id,
            ),
        )
        await db.commit()
        return {
            "id": sid,
            "agent_id": agent_id,
            "identity_id": identity_id,
            "model_profile": model_profile,
            "title": title,
            "created_at": now,
            "session_kind": "side",
            "parent_session_id": parent_session_id,
            "snapshot_cutoff_message_id": snapshot_cutoff_message_id,
            "message_count": 0,
        }

    async def promote_side(
        self,
        session_id: str,
        agent_id: str,
        *,
        title: str | None = None,
        context_summary: str | None = None,
    ) -> dict | None:
        db = await get_app_db()
        updates = ["session_kind='main'"]
        params: list[object] = []
        if title is not None:
            updates.append("title=?")
            params.append(title)
        if context_summary is not None:
            updates.append("context_summary=?")
            params.append(context_summary)
            updates.append("context_summary_cutoff=0")
        params.extend([session_id, agent_id])
        cursor = await db.execute(
            f"UPDATE sessions SET {', '.join(updates)} "
            "WHERE id=? AND agent_id=? AND session_kind='side'",
            params,
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
        return await self.get_owned(session_id, agent_id)

    async def cleanup_stale_sides(self, *, max_age_days: int = 7) -> list[str]:
        """Delete unpromoted side sessions with no updates older than ``max_age_days``."""
        db = await get_app_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = await db.execute_fetchall(
            "SELECT s.id FROM sessions s WHERE s.session_kind='side' "
            "AND COALESCE((SELECT MAX(m.created_at) FROM messages m WHERE m.session_id=s.id), s.created_at) < ?",
            (cutoff,),
        )
        stale_ids = [str(row["id"]) for row in rows]
        if not stale_ids:
            return []
        placeholders = ",".join("?" for _ in stale_ids)
        await db.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", stale_ids)
        await db.commit()
        return stale_ids

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
            "SELECT id, agent_id, identity_id, model_profile, title, created_at, "
            "  session_kind, parent_session_id, snapshot_cutoff_message_id, "
            "  (SELECT count(*) FROM messages m WHERE m.session_id=sessions.id) as message_count "
            "FROM sessions WHERE id=? AND agent_id=?",
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

    async def get_messages(
        self,
        session_id: str,
        limit: int = 0,
        offset: int = 0,
        max_content_bytes: int | None = None,
    ) -> list[dict]:
        db = await get_app_db()
        # 0 historically meant "everything", which buffers a whole long session
        # into memory on every call.  Treat it as a bounded default; callers that
        # genuinely need the full transcript pass an explicit limit.
        effective_limit = limit if limit > 0 else 200
        if max_content_bytes is None:
            rows = await db.execute_fetchall(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (session_id, effective_limit, offset),
            )
            return [dict(r) for r in rows]
        # Byte-bounded fetch: stop accumulating once the conversation's content
        # exceeds the budget, so an enormous session cannot be buffered whole.
        rows: list[dict] = []
        total = 0
        cursor = await db.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (session_id, effective_limit, offset),
        )
        async for row in cursor:
            content = row["content"] if isinstance(row["content"], str) else ""
            total += len(content)
            if total > max_content_bytes:
                break
            rows.append(dict(row))
        await cursor.close()
        return rows

    async def count_messages(
        self, session_id: str, before_message_id: str | None = None
    ) -> int:
        """Total messages, or how many precede ``before_message_id``.

        ``context_summary_cutoff`` is an absolute count from the start of the
        session, so turning it into an offset into a bounded window needs the
        window's absolute position.  Counting DB-side keeps that O(1) in memory
        no matter how long the session is.
        """
        db = await get_app_db()
        if before_message_id is None:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?",
                (session_id,),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=? AND rowid < "
                "(SELECT rowid FROM messages WHERE session_id=? AND id=?)",
                (session_id, session_id, before_message_id),
            )
        return int(rows[0]["n"]) if rows else 0

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
            "(SELECT rowid FROM messages WHERE id=? AND session_id=?) "
            "ORDER BY created_at ASC LIMIT ?",
            (session_id, message_id, session_id, limit),
        )
        return [dict(r) for r in rows]

    async def get_messages_before(
        self, session_id: str, message_id: str, limit: int
    ) -> list[dict]:
        """Rows strictly before ``message_id`` (by rowid), bounded, ascending."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? AND rowid < "
            "(SELECT rowid FROM messages WHERE id=? AND session_id=?) "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, message_id, session_id, limit),
        )
        rows.reverse()
        return [dict(r) for r in rows]

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        kind: str = "normal",
        parent_message_id: str | None = None,
    ) -> dict:
        db = await get_app_db()
        mid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO messages (id, session_id, role, content, kind, parent_message_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, session_id, role, content, kind, parent_message_id, now),
        )
        await db.commit()
        return {
            "id": mid,
            "session_id": session_id,
            "role": role,
            "content": content,
            "kind": kind,
            "parent_message_id": parent_message_id,
            "created_at": now,
        }

    async def get_steering_messages(
        self,
        session_id: str,
        parent_message_id: str,
    ) -> list[dict]:
        """Return applied steering messages bound to one user turn, ascending."""
        db = await get_app_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE session_id=? AND role='user' "
            "AND kind='steering' AND parent_message_id=? ORDER BY created_at ASC",
            (session_id, parent_message_id),
        )
        return [dict(r) for r in rows]

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
            "SELECT MIN(rowid) FROM messages WHERE session_id=? AND role='user' "
            "AND kind='normal' AND rowid > ?"
            "), 9223372036854775807)",
            (session_id, rows[0]["rowid"], session_id, rows[0]["rowid"]),
        )
        await db.commit()
        return cursor.rowcount
