from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator

from fastapi import HTTPException

from engine.execution import (
    EngineRequest,
    RunStateError,
    RunStateStore,
    RunStatus,
    STEERING_BROKER,
    raw_text_delta,
    run_stream_with_runtime as engine_run_stream_with_runtime,
    resume_stream_with_runtime as engine_resume_stream_with_runtime,
)
from engine.context import (
    CONTEXT_DISPLAY_WINDOW,
    SESSION_SUMMARY_PREFIX,
    summarize_session,
)
from engine.identity import IdentityCatalog, IdentityCatalogError
from engine.llm.model_config import resolve_llm_config
from engine.llm.observability import generation_context
from engine.observability.trace_store import _redact_secrets_in_text
from common.yaml_utils import YamlConfigError

from ..schemas.session import ContextCompressionOut, SessionOut, MessageOut, SteerOut
from ..infrastructure.repositories.session_repo import SessionRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from .engine_runtime import build_engine_runtime, close_session_mcp_clients
from .token_stats_service import TokenStatsService

# Two concurrent streams on the same session both read the recent history and
# then both insert their user message, so each run's context omits the other's
# just-inserted turn and the two assistant replies interleave into a corrupted
# conversation.  Serialize turns per session: a fast retry queues behind the
# in-flight turn instead of cross-wiring it.
#
# The map is guarded by a threading lock so it stays safe even if the server
# ever runs the handlers from more than one thread/loop, and it is bounded: an
# abandoned (never-deleted) session must not leak a lock per session forever.
# Eviction is refcounted, NOT keyed on asyncio.Lock.locked(): locked() reports
# False in the window between release() and a queued waiter's wake-up, so
# evicting on it could drop a lock that a waiter is about to acquire and let two
# turns interleave the exact history-read/insert window this serializes.  A
# caller (holder or queued waiter) is counted in-use from before acquire() until
# after release(), and only zero-user entries are evicted.
_SESSION_STREAM_LOCKS: dict[str, asyncio.Lock] = {}
_SESSION_STREAM_LOCK_USERS: dict[str, int] = {}
_SESSION_STREAM_LOCKS_GUARD = threading.Lock()
_SESSION_STREAM_LOCKS_MAX = 64


def _acquire_session_lock_slot(session_id: str) -> asyncio.Lock:
    """Reserve this session's lock and mark the caller in-use before acquiring."""
    with _SESSION_STREAM_LOCKS_GUARD:
        lock = _SESSION_STREAM_LOCKS.get(session_id)
        if lock is None:
            if len(_SESSION_STREAM_LOCKS) >= _SESSION_STREAM_LOCKS_MAX:
                for key in list(_SESSION_STREAM_LOCKS):
                    if _SESSION_STREAM_LOCK_USERS.get(key, 0) == 0:
                        del _SESSION_STREAM_LOCKS[key]
                        _SESSION_STREAM_LOCK_USERS.pop(key, None)
            lock = asyncio.Lock()
            _SESSION_STREAM_LOCKS[session_id] = lock
        _SESSION_STREAM_LOCK_USERS[session_id] = _SESSION_STREAM_LOCK_USERS.get(session_id, 0) + 1
        return lock


def _release_session_lock_slot(session_id: str) -> None:
    with _SESSION_STREAM_LOCKS_GUARD:
        remaining = _SESSION_STREAM_LOCK_USERS.get(session_id, 0) - 1
        if remaining > 0:
            _SESSION_STREAM_LOCK_USERS[session_id] = remaining
        else:
            _SESSION_STREAM_LOCK_USERS.pop(session_id, None)


@asynccontextmanager
async def _session_turn_lock(session_id: str) -> AsyncIterator[None]:
    """Serialize the per-session turn-mutation window, refcounting the slot.

    Held only for the history-read + user-insert window, never across the
    streaming generator, so an abandoned stream cannot deadlock a session.
    """
    lock = _acquire_session_lock_slot(session_id)
    try:
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
    finally:
        _release_session_lock_slot(session_id)


# Recent messages passed to the engine as short-term conversational context.
# Only visible reply text is stored per turn — tool calls, file reads and
# command output never reach the next turn — so even a turn that ran dozens of
# tools costs one short message here.  At 10 (5 exchanges) a working session
# lost sight of its own opening question long before the work was done.  The
# engine's context fitter still compacts whatever does not fit the window.
_HISTORY_LIMIT = 40
_SIDE_SNAPSHOT_MESSAGE_LIMIT = 16
_SIDE_SNAPSHOT_MAX_CHARS = 12_000
_SIDE_SNAPSHOT_PREFIX = "[Read-only snapshot of main conversation]\n"


def _redact_snapshot_text(text: str) -> str:
    """Redact both pattern-matched secrets and configured Vault literals."""
    if not text:
        return ""
    redacted = _redact_secrets_in_text(text)
    try:
        from common import config as common_config
        from common.vault import VaultRedactor, VaultStore

        vault = VaultStore(common_config.PATHS.data_dir / "vault.json")
        entries = vault.all_entries()
        if entries:
            redacted, _ = VaultRedactor.redact(redacted, all_entries=entries)
    except Exception:
        logger.warning("vault redaction skipped for snapshot", exc_info=True)
    return redacted
# Upper bound for one compress call; far beyond any real session while still
# bounding the row count (compression already loads the conversation into memory).
_COMPRESS_MESSAGE_CAP = 50_000
# Byte budget for a compress call: stop fetching once the conversation content
# exceeds this, so an enormous session cannot be buffered whole and sent to the
# summarizer.
_COMPRESS_BYTE_CAP = 20_000_000
logger = logging.getLogger(__name__)


class SessionService:

    def __init__(
        self,
        session_repo: SessionRepo,
        agent_profile_repo: AgentProfileRepo,
        *,
        identity_catalog: IdentityCatalog | None = None,
        token_stats_service: TokenStatsService | None = None,
        run_state_store: RunStateStore | None = None,
    ) -> None:
        self.session_repo = session_repo
        self.agent_profile_repo = agent_profile_repo
        self.identity_catalog = identity_catalog
        self.token_stats_service = token_stats_service
        self.run_state_store = run_state_store

    async def list_sessions(self, agent_id: str) -> list[SessionOut]:
        rows = await self.session_repo.list_by_agent(agent_id)
        return [SessionOut(**r) for r in rows]

    def _catalog(self) -> IdentityCatalog:
        if self.identity_catalog is not None:
            return self.identity_catalog
        from .engine_runtime import load_runtime_identity_catalog

        return load_runtime_identity_catalog()

    def _validate_identity(self, identity_id: str) -> None:
        try:
            self._catalog().get(identity_id)
        except IdentityCatalogError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def create_session(
        self,
        agent_id: str,
        title: str,
        identity_id: str | None = None,
        model_profile: str | None = None,
    ) -> SessionOut:
        profile = await self.agent_profile_repo.get(agent_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")
        if identity_id is not None:
            self._validate_identity(identity_id)
        self._validate_model_profile(model_profile)
        row = await self.session_repo.create(
            agent_id,
            title or "新对话",
            identity_id,
            model_profile,
        )
        return SessionOut(**row)

    @staticmethod
    def _validate_model_profile(model_profile: str | None) -> None:
        if model_profile is None:
            return
        try:
            resolve_llm_config(model_profile=model_profile)
        except YamlConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def _resolve_session_identity(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        requested_identity_id: str | None,
    ) -> str:
        session = await self.session_repo.get_owned(session_id, agent_id)
        if session is None:
            raise HTTPException(404, "Session not found")

        pinned_identity_id = session.get("identity_id")
        if requested_identity_id is not None:
            self._validate_identity(requested_identity_id)
            if pinned_identity_id and pinned_identity_id != requested_identity_id:
                raise HTTPException(
                    409,
                    "This session is already bound to a different identity. Create a new session to switch identity.",
                )
            selected_identity_id = requested_identity_id
        elif pinned_identity_id:
            selected_identity_id = str(pinned_identity_id)
            self._validate_identity(selected_identity_id)
        else:
            # This is a lasting session preference for direct ReAct, not an
            # intent router.  SkillChain routing happens independently in the
            # Engine for every message.
            selected_identity_id = self._catalog().default.id

        if not pinned_identity_id:
            claimed = await self.session_repo.claim_identity(
                session_id,
                agent_id,
                selected_identity_id,
            )
            if not claimed:
                # A concurrent first message won the race; use its stable binding.
                concurrent_session = await self.session_repo.get_owned(session_id, agent_id)
                if concurrent_session is None or not concurrent_session.get("identity_id"):
                    raise HTTPException(409, "Unable to establish a stable session identity")
                selected_identity_id = str(concurrent_session["identity_id"])
                self._validate_identity(selected_identity_id)
                if requested_identity_id is not None and selected_identity_id != requested_identity_id:
                    raise HTTPException(
                        409,
                        "This session was bound to a different identity by another request.",
                    )

        return selected_identity_id

    async def _recent_history(self, session_id: str) -> list[dict]:
        """Last N messages as {"role","content"} dicts for engine short-term context."""
        context_reader = getattr(self.session_repo, "get_context", None)
        context = await context_reader(session_id) if context_reader is not None else {}
        summary = context.get("context_summary") if isinstance(context, dict) else ""
        cutoff = context.get("context_summary_cutoff", 0) if isinstance(context, dict) else 0
        if summary:
            history = [{"role": "user", "content": f"{SESSION_SUMMARY_PREFIX}{summary}"}]
            if isinstance(cutoff, int) and cutoff > 0:
                # cutoff counts from the start of the session, so it cannot be
                # paged from directly: get_messages caps a page at 200, and
                # taking the last rows of that page yields cutoff+190..cutoff+199
                # rather than the newest messages.  Offset from the end instead,
                # never reaching back into the summarized prefix.
                counter = getattr(self.session_repo, "count_messages", None)
                total = await counter(session_id) if counter is not None else 0
                start = max(cutoff, total - _HISTORY_LIMIT)
                rows = await self.session_repo.get_messages(
                    session_id, limit=_HISTORY_LIMIT, offset=start
                )
            else:
                rows = await self.session_repo.get_recent_messages(session_id, _HISTORY_LIMIT)
        else:
            history = []
            rows = await self.session_repo.get_recent_messages(session_id, _HISTORY_LIMIT)
        return history + [
            {"role": r["role"], "content": r["content"]}
            for r in rows
        ]

    async def _history_before_message(
        self,
        session_id: str,
        message_id: str,
    ) -> list[dict]:
        """Build bounded history preceding the exact user message being resumed."""
        prior_rows = await self.session_repo.get_messages_before(
            session_id, message_id, limit=_HISTORY_LIMIT
        )
        context_reader = getattr(self.session_repo, "get_context", None)
        context = await context_reader(session_id) if context_reader is not None else {}
        summary = context.get("context_summary") if isinstance(context, dict) else ""
        cutoff = context.get("context_summary_cutoff", 0) if isinstance(context, dict) else 0
        history: list[dict] = []
        if isinstance(summary, str) and summary:
            history.append({"role": "user", "content": f"{SESSION_SUMMARY_PREFIX}{summary}"})
            if isinstance(cutoff, int) and cutoff > 0 and prior_rows:
                # cutoff is absolute; prior_rows is a window of at most
                # _HISTORY_LIMIT rows ending at message_id.  Slicing by the raw
                # cutoff emptied the window for any cutoff >= its length.
                # Translate to a window-relative index via the window's own
                # absolute start.
                counter = getattr(self.session_repo, "count_messages", None)
                window_start = (
                    await counter(session_id, prior_rows[0].get("id"))
                    if counter is not None
                    else cutoff
                )
                drop = max(0, cutoff - window_start)
                prior_rows = prior_rows[drop:]
        return history + [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in prior_rows[-_HISTORY_LIMIT:]
        ]

    async def _build_runtime(self, agent_id: str, profile_name: str, session_id: str):
        session = await self.session_repo.get_owned(session_id, agent_id)
        model_profile = session.get("model_profile") if session else None
        kwargs = {"session_id": session_id}
        if model_profile:
            kwargs["model_profile"] = model_profile
        return build_engine_runtime(agent_id, profile_name, **kwargs)

    async def update_model_profile(
        self,
        agent_id: str,
        session_id: str,
        model_profile: str | None,
    ) -> SessionOut:
        self._validate_model_profile(model_profile)
        row = await self.session_repo.update_model_profile(session_id, agent_id, model_profile)
        if row is None:
            raise HTTPException(404, "Session not found")
        return SessionOut(**row)

    async def _cleanup_session_resources(self, session_id: str) -> None:
        from common import config as common_config
        from engine.safety.taint import delete_session_taint

        delete_session_taint(common_config.PATHS.agent_dir, session_id)
        await close_session_mcp_clients(session_id)
        # Drop the per-session stream lock so deleted sessions do not leak a lock
        # for the server's lifetime.  Under the guard, and only when idle: an
        # unguarded pop racing an in-flight turn (or one popping a lock a waiter
        # still holds a slot on) breaks the same serialization the map provides.
        # A busy slot is left for refcount eviction once its turn drains.
        with _SESSION_STREAM_LOCKS_GUARD:
            if _SESSION_STREAM_LOCK_USERS.get(session_id, 0) == 0:
                _SESSION_STREAM_LOCKS.pop(session_id, None)
                _SESSION_STREAM_LOCK_USERS.pop(session_id, None)

    async def delete_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        expected_kind: str | None = None,
    ) -> None:
        if expected_kind is not None:
            owned = await self.session_repo.get_owned(session_id, agent_id)
            if owned is None:
                raise HTTPException(404, "Session not found")
            if owned.get("session_kind", "main") != expected_kind:
                raise HTTPException(
                    409,
                    f"Refused to delete {owned.get('session_kind', 'main')} session as {expected_kind}",
                )
        list_sides = getattr(self.session_repo, "list_child_sides", None)
        child_side_ids = (
            await list_sides(session_id, agent_id)
            if callable(list_sides)
            else []
        )
        deleted = await self.session_repo.delete_owned(session_id, agent_id)
        if not deleted:
            raise HTTPException(404, "Session not found")
        for side_id in child_side_ids:
            await self.session_repo.delete_owned(side_id, agent_id)
            await self._cleanup_session_resources(side_id)
        await self._cleanup_session_resources(session_id)

    async def cleanup_stale_sides(self, *, max_age_days: int = 7) -> list[str]:
        """Delete stale unpromoted side sessions and release their taint/MCP/lock state."""
        stale_ids = await self.session_repo.cleanup_stale_sides(max_age_days=max_age_days)
        for side_id in stale_ids:
            await self._cleanup_session_resources(side_id)
        return stale_ids

    async def _build_parent_snapshot(
        self,
        parent_session_id: str,
        cutoff_message_id: str | None = None,
    ) -> str:
        """Build a bounded, secret- and vault-redacted read-only snapshot of the parent session."""
        parts: list[str] = []
        context_reader = getattr(self.session_repo, "get_context", None)
        context = await context_reader(parent_session_id) if context_reader is not None else {}
        summary = context.get("context_summary") if isinstance(context, dict) else ""
        if isinstance(summary, str) and summary.strip():
            parts.append(f"Summary:\n{_redact_snapshot_text(summary.strip())}")

        rows: list[dict] = []
        if cutoff_message_id:
            cutoff_msg = await self.session_repo.get_message(parent_session_id, cutoff_message_id)
            if cutoff_msg is not None:
                prior = await self.session_repo.get_messages_before(
                    parent_session_id,
                    cutoff_message_id,
                    limit=_SIDE_SNAPSHOT_MESSAGE_LIMIT - 1,
                )
                rows = [*prior, cutoff_msg]
        if not rows:
            rows = await self.session_repo.get_recent_messages(
                parent_session_id,
                _SIDE_SNAPSHOT_MESSAGE_LIMIT,
            )

        if rows:
            lines: list[str] = []
            for row in rows:
                role = str(row.get("role") or "user")
                body = _redact_snapshot_text(str(row.get("content") or "")).strip()
                if not body:
                    continue
                if len(body) > 1500:
                    body = body[:1500] + "…"
                lines.append(f"{role}: {body}")
            if lines:
                parts.append("Recent turns:\n" + "\n".join(lines))

        snapshot = "\n\n".join(parts).strip()
        if len(snapshot) > _SIDE_SNAPSHOT_MAX_CHARS:
            snapshot = snapshot[-_SIDE_SNAPSHOT_MAX_CHARS:]
        return snapshot

    @staticmethod
    def _merge_parent_taint(parent_session_id: str, child_session_id: str) -> None:
        """Monotonically propagate parent taint sources into a side or promoted session."""
        if not parent_session_id or not child_session_id:
            return
        try:
            from common import config as common_config
            from engine.safety.taint import SessionTaintStore

            state_dir = common_config.PATHS.agent_dir
            parent_taint = SessionTaintStore(state_dir=state_dir, session_id=parent_session_id)
            if parent_taint.is_tainted:
                child_taint = SessionTaintStore(state_dir=state_dir, session_id=child_session_id)
                child_taint.merge_from(parent_taint.sources)
        except Exception:
            logger.warning(
                "failed to propagate parent taint (parent=%s, child=%s)",
                parent_session_id,
                child_session_id,
                exc_info=True,
            )

    async def create_side_session(
        self,
        agent_id: str,
        parent_session_id: str | None = None,
        *,
        title: str = "Side chat",
        reuse_existing: bool = True,
    ) -> SessionOut:
        """Create or reuse an isolated read-only /side session anchored to a parent main session."""
        profile = await self.agent_profile_repo.get(agent_id)
        if profile is None:
            raise HTTPException(404, "Agent profile not found")

        identity_id: str | None = None
        model_profile: str | None = None
        cutoff_message_id: str | None = None
        if parent_session_id is not None:
            parent = await self.session_repo.get_owned(parent_session_id, agent_id)
            if parent is None:
                raise HTTPException(404, "Parent session not found")
            if parent.get("session_kind") == "side":
                raise HTTPException(409, "Cannot create a side chat inside another side chat")
            if reuse_existing:
                existing = await self.session_repo.get_active_side(agent_id, parent_session_id)
                if existing is not None:
                    self._merge_parent_taint(parent_session_id, str(existing["id"]))
                    return SessionOut(**existing)
            identity_id = parent.get("identity_id")
            model_profile = parent.get("model_profile")
            recent = await self.session_repo.get_recent_messages(parent_session_id, 1)
            if recent:
                cutoff_message_id = str(recent[-1]["id"])

        row = await self.session_repo.create_side(
            agent_id,
            parent_session_id=parent_session_id,
            snapshot_cutoff_message_id=cutoff_message_id,
            title=title or "Side chat",
            identity_id=identity_id,
            model_profile=model_profile,
        )
        if parent_session_id is not None:
            self._merge_parent_taint(parent_session_id, str(row["id"]))
        return SessionOut(**row)

    async def promote_side_session(
        self,
        agent_id: str,
        side_session_id: str,
        *,
        title: str | None = None,
        keep_parent_context: bool = True,
    ) -> SessionOut:
        """Promote a /side session into a standalone main session with redacted context & taint monotonicity."""
        side = await self.session_repo.get_owned(side_session_id, agent_id)
        if side is None:
            raise HTTPException(404, "Session not found")
        if side.get("session_kind") != "side":
            raise HTTPException(409, "Session is already a main session")

        derived_title = title.strip() if isinstance(title, str) and title.strip() else None
        if derived_title is None:
            side_msgs = await self.session_repo.get_messages(side_session_id, limit=5)
            first_user = next(
                (
                    m["content"]
                    for m in side_msgs
                    if m.get("role") == "user" and str(m.get("content", "")).strip()
                ),
                None,
            )
            if isinstance(first_user, str):
                derived_title = _redact_snapshot_text(first_user).splitlines()[0][:40] or "Promoted session"
            else:
                derived_title = side.get("title") or "Promoted session"

        parent_id = side.get("parent_session_id")
        persisted_summary: str | None = None
        if isinstance(parent_id, str) and parent_id:
            # Always propagate parent taint on /promote regardless of
            # keep_parent_context: prior turns in the side chat already saw
            # the parent snapshot, so keep_parent_context=False cannot launder it.
            self._merge_parent_taint(parent_id, side_session_id)
            if keep_parent_context:
                snapshot = await self._build_parent_snapshot(
                    parent_id,
                    cutoff_message_id=side.get("snapshot_cutoff_message_id"),
                )
                if snapshot:
                    persisted_summary = snapshot

        promoted = await self.session_repo.promote_side(
            side_session_id,
            agent_id,
            title=derived_title,
            context_summary=persisted_summary,
        )
        if promoted is None:
            raise HTTPException(409, "Session could not be promoted")
        return SessionOut(**promoted)

    async def compress_session(self, agent_id: str, session_id: str) -> ContextCompressionOut:
        session = await self.session_repo.get_owned(session_id, agent_id)
        if session is None:
            raise HTTPException(404, "Session not found")

        # Compression summarizes the WHOLE conversation, so fetch it all
        # explicitly (the get_messages default cap of 200 would silently drop the
        # tail), bounded by both row count and content bytes so a pathological
        # session cannot be buffered whole.
        rows = await self.session_repo.get_messages(
            session_id,
            limit=_COMPRESS_MESSAGE_CAP,
            max_content_bytes=_COMPRESS_BYTE_CAP,
        )
        if not rows:
            raise HTTPException(400, "Cannot compress an empty session")

        profile = await self.agent_profile_repo.get(agent_id)
        profile_name = profile["name"] if profile else "Agent"
        _runtime, services = await self._build_runtime(agent_id, profile_name, session_id)
        try:
            # Manual /compress runs outside any agent run, so nothing has
            # installed a generation scope.  Without this the compaction's own
            # LLM call lands in llm_generations with a NULL session_id we are
            # holding right here.
            with generation_context(session_id=session_id):
                summary_result = await summarize_session(
                    [{"role": row["role"], "content": row["content"]} for row in rows],
                    services.llm,
                )
        finally:
            close = getattr(services, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        if not summary_result.usable:
            raise HTTPException(
                422,
                "Model did not return a usable context summary "
                f"({summary_result.status.value})",
            )
        summary = summary_result.summary

        await self.session_repo.set_context(session_id, summary, len(rows))
        return ContextCompressionOut(
            session_id=session_id,
            summary=summary,
            message_count=len(rows),
            context_summary_cutoff=len(rows),
        )

    async def list_messages(
        self,
        agent_id: str,
        session_id: str,
        limit: int = 0,
        offset: int = 0,
    ) -> list[MessageOut]:
        session = await self.session_repo.get_owned(session_id, agent_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        rows = await self.session_repo.get_messages(session_id, limit=limit, offset=offset)
        return [MessageOut(**r) for r in rows]

    async def resume_run(
        self,
        agent_id: str,
        run_id: str,
    ) -> AsyncGenerator[dict, None]:
        """Resume an incomplete run through the same SSE/session contract."""
        stream = await self.prepare_resume_run(agent_id, run_id)
        async for event in stream:
            yield event

    async def prepare_resume_run(
        self,
        agent_id: str,
        run_id: str,
    ) -> AsyncGenerator[dict, None]:
        """Validate recovery and discard stale output before opening an SSE response."""
        try:
            if self.run_state_store is None:
                raise HTTPException(503, "Run state is temporarily unavailable")
            state = self.run_state_store.get(run_id)
        except (ValueError, RunStateError) as exc:
            raise HTTPException(404, "Run not found") from exc
        if state is None or state.agent_id != agent_id or not state.session_id:
            raise HTTPException(404, "Run not found")
        if state.status not in {
            RunStatus.INCOMPLETE,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise HTTPException(409, f"Run cannot be resumed from {state.status.value}")
        if not state.identity_id:
            raise HTTPException(409, "Run is missing its execution identity")
        self._validate_identity(state.identity_id)

        if not state.message_id:
            raise HTTPException(
                409,
                "Run predates message-bound resume and cannot be resumed safely",
            )
        user_message = await self.session_repo.get_message(state.session_id, state.message_id)
        if (
            user_message is None
            or user_message.get("role") != "user"
            or not isinstance(user_message.get("content"), str)
        ):
            raise HTTPException(409, "Run message is no longer available for resume")
        # A newer user turn after this run's message means the session moved past
        # it; the check is bounded to the tail, which is all that can invalidate
        # a resume.
        after_rows = await self.session_repo.get_messages_since(
            state.session_id, state.message_id, limit=20
        )
        if any(
            row.get("role") == "user" and row.get("kind", "normal") == "normal"
            for row in after_rows
        ):
            raise HTTPException(
                409,
                "Run has a newer user turn and cannot be resumed into this session safely",
            )
        get_steering = getattr(self.session_repo, "get_steering_messages", None)
        steering_rows = (
            await get_steering(state.session_id, state.message_id)
            if callable(get_steering)
            else []
        )
        steering_history = tuple(
            str(row["content"])
            for row in steering_rows
            if isinstance(row.get("content"), str) and row["content"].strip()
        )
        history = await self._history_before_message(
            state.session_id,
            state.message_id,
        )
        # This completes ownership and identity validation synchronously, before
        # any state is mutated.  The stale partial assistant output is discarded
        # only once the resumed stream actually produces a replacement reply
        # (see _stream_message's finally), so a failed resume never loses it.
        return await self.prepare_stream_message(
            agent_id,
            state.session_id,
            user_message["content"],
            skill_name=state.forced_skill,
            working_dir=state.working_dir,
            _history_override=history,
            _resume_run_id=run_id,
            _message_id=state.message_id,
            _execution_identity_id=state.identity_id,
            _steering_history=steering_history,
        )

    async def steer_session(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        *,
        run_id: str | None = None,
    ) -> SteerOut:
        """Inject a mid-turn steering message into an active run without acquiring turn lock."""
        if not await self.session_repo.exists(session_id, agent_id):
            raise HTTPException(404, "Session not found")

        active_id = STEERING_BROKER.active_run_id(session_id)
        waiting_approval = False
        if active_id and self.run_state_store is not None:
            try:
                state = self.run_state_store.get(active_id)
                waiting_approval = (
                    state is not None and state.status is RunStatus.WAITING_APPROVAL
                )
            except Exception:
                waiting_approval = False

        res = STEERING_BROKER.submit(
            session_id=session_id,
            content=content,
            run_id=run_id,
            waiting_approval=waiting_approval,
        )
        return SteerOut(
            accepted=res.accepted,
            status=res.status,
            run_id=res.run_id,
            steer_id=res.steer_id,
            pending_count=res.pending_count,
        )

    async def prepare_stream_message(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        context: str | None = None,
        skill_name: str | None = None,
        identity_id: str | None = None,
        working_dir: str | None = None,
        *,
        _history_override: list[dict] | None = None,
        _resume_run_id: str | None = None,
        _message_id: str | None = None,
        _execution_identity_id: str | None = None,
        _steering_history: tuple[str, ...] = (),
    ) -> AsyncGenerator[dict, None]:
        """Validate the session before an HTTP caller commits to an SSE 200."""
        selected_identity_id = await self._resolve_session_identity(
            agent_id,
            session_id,
            content,
            identity_id,
        )
        profile = await self.agent_profile_repo.get(agent_id)
        profile_name = profile["name"] if profile else "Agent"
        return self._stream_message(
            agent_id,
            session_id,
            content,
            selected_identity_id=selected_identity_id,
            profile_name=profile_name,
            context=context,
            skill_name=skill_name,
            working_dir=working_dir,
            _history_override=_history_override,
            _resume_run_id=_resume_run_id,
            _message_id=_message_id,
            execution_identity_id=_execution_identity_id,
            steering_history=_steering_history,
        )

    async def _stream_message(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        *,
        selected_identity_id: str,
        profile_name: str,
        context: str | None = None,
        skill_name: str | None = None,
        working_dir: str | None = None,
        _history_override: list[dict] | None = None,
        _resume_run_id: str | None = None,
        _message_id: str | None = None,
        execution_identity_id: str | None = None,
        steering_history: tuple[str, ...] = (),
    ) -> AsyncGenerator[dict, None]:
        """Yield SSE event dicts. Streams text chunks as they arrive from the engine."""
        # Stream structured events from engine
        def sse(event: str, data: dict) -> dict:
            return {"event": event, "data": json.dumps(data, ensure_ascii=False)}

        full_reply: list[str] = []
        visible_raw_reply: list[str] = []
        visible_provisional_reply: dict[str, list[str]] = {}
        msg: dict | None = None
        run_id: str | None = None
        run = None
        terminal_status = "completed"
        terminal_reason: str | None = None
        terminal_notice: str | None = None
        try:
            # Serialize history-read + user-message insert: two concurrent turns
            # both read before either inserts, so each run's context would omit
            # the other's just-inserted user turn.
            async with _session_turn_lock(session_id):
                if _resume_run_id is None:
                    # Fetch recent history BEFORE saving the new message (avoids duplication)
                    history = await self._recent_history(session_id)
                    user_message = await self.session_repo.add_message(session_id, "user", content)
                    message_id = str(user_message["id"])
                else:
                    history = list(_history_override or [])
                    message_id = _message_id
            session_meta = await self.session_repo.get_owned(session_id, agent_id)
            is_side = bool(session_meta and session_meta.get("session_kind") == "side")
            if is_side:
                parent_id = session_meta.get("parent_session_id") if session_meta else None
                if isinstance(parent_id, str) and parent_id:
                    # H2: Inherit parent taint on every side run before runtime
                    # preparation loads SessionTaintStore, preventing a tainted
                    # parent's snapshot from leaking through side web_fetch/web_search.
                    self._merge_parent_taint(parent_id, session_id)
                    if _resume_run_id is None:
                        snapshot = await self._build_parent_snapshot(
                            parent_id,
                            cutoff_message_id=(
                                session_meta.get("snapshot_cutoff_message_id")
                                if session_meta
                                else None
                            ),
                        )
                        if snapshot:
                            history = [
                                {"role": "user", "content": f"{_SIDE_SNAPSHOT_PREFIX}{snapshot}"},
                                *history,
                            ]
            runtime, services = await self._build_runtime(agent_id, profile_name, session_id)
            model_name = str(getattr(getattr(services, "llm", None), "model", "") or "")
            request = EngineRequest(
                message=content,
                history=history,
                context=context,
                forced_skill=skill_name,
                identity_id=selected_identity_id,
                execution_identity_id=execution_identity_id,
                working_dir=working_dir,
                message_id=message_id,
                steering_history=steering_history,
                ephemeral=is_side,
                read_only_tools=is_side,
            )
            if _resume_run_id is None:
                # RunStateStore.create() fsyncs; keep the blocking I/O off the
                # event loop like the rest of the S1 persistence changes.
                run = await asyncio.to_thread(
                    engine_run_stream_with_runtime, request, runtime, services
                )
            else:
                run = await asyncio.to_thread(
                    engine_resume_stream_with_runtime,
                    request,
                    runtime,
                    services,
                    _resume_run_id,
                )
            run_id = getattr(run, "run_id", None)
            async for ev in run.stream_events():
                t = ev.type.value
                if t == "run_started":
                    run_id = str(ev.data.get("run_id") or run_id or "") or None
                    yield sse("run_started", {"run_id": run_id})
                elif t == "raw_response_event":
                    delta = raw_text_delta(ev, include_provisional=False)
                    if delta is not None:
                        visible_raw_reply.append(delta)
                        yield sse("message", {"text": delta})
                elif t == "smith_ui":
                    # The execution boundary only emits normalized smith-ui
                    # payloads; the Server is a transport adapter here.
                    yield sse("smith_ui", ev.data)
                elif t == "smith_ui_fallback":
                    yield sse("smith_ui_fallback", ev.data)
                elif t == "provisional_text_delta":
                    provision_id = str(ev.data.get("provision_id", ""))
                    text = ev.data.get("text")
                    if provision_id and isinstance(text, str) and text:
                        visible_provisional_reply.setdefault(provision_id, []).append(text)
                        yield sse("provisional_text_delta", {
                            "provision_id": provision_id,
                            "text": text,
                        })
                elif t == "provisional_commit":
                    provision_id = str(ev.data.get("provision_id", ""))
                    if provision_id:
                        yield sse("provisional_commit", {"provision_id": provision_id})
                elif t == "provisional_retract":
                    provision_id = str(ev.data.get("provision_id", ""))
                    if provision_id:
                        visible_provisional_reply.pop(provision_id, None)
                        payload = {"provision_id": provision_id}
                        reason = ev.data.get("reason")
                        if isinstance(reason, str) and reason:
                            payload["reason"] = reason
                        yield sse("provisional_retract", payload)
                elif t == "text_delta":
                    chunk = ev.data.get("text", "")
                    full_reply.append(chunk)
                    if not ev.data.get("already_streamed"):
                        yield sse("message", {"text": chunk})
                elif t == "steering_applied":
                    items = ev.data.get("items")
                    if isinstance(items, list):
                        for item in items:
                            item_content = item.get("content") if isinstance(item, dict) else None
                            if isinstance(item_content, str) and item_content.strip():
                                try:
                                    await self.session_repo.add_message(
                                        session_id,
                                        "user",
                                        item_content,
                                        kind="steering",
                                        parent_message_id=message_id,
                                    )
                                except Exception:
                                    logger.warning(
                                        "failed to persist steering message (session=%s)",
                                        session_id,
                                        exc_info=True,
                                    )
                    yield sse("steering_applied", {
                        "steer_ids": ev.data.get("steer_ids", []),
                        "count": ev.data.get("count", 0),
                        "items": ev.data.get("items", []),
                        "iteration": ev.data.get("iteration", 0),
                    })
                elif t == "steering_deferred":
                    yield sse("steering_deferred", {
                        "steer_ids": ev.data.get("steer_ids", []),
                        "items": ev.data.get("items", []),
                    })
                elif t == "thinking":
                    yield sse("thinking", {"text": ev.data.get("text", ""), "done": bool(ev.data.get("done"))})
                elif t == "tool_call_start":
                    args = ev.data.get("arguments") or {}
                    hint = args.get("path") or args.get("file_path") or args.get("query") or args.get("command", "")
                    # The tool arguments are model-authored and often contain
                    # credentials the model read from the workspace (e.g. a
                    # `curl -H "Authorization: Bearer ..."`).  Redact the same
                    # credential shapes the trace store redacts so secrets never
                    # stream to the client.
                    yield sse("tool_call", {"id": ev.data.get("id", ""), "name": ev.data.get("name", ""), "hint": _redact_secrets_in_text(str(hint))[:120]})
                elif t == "tool_call_result":
                    presentation = ev.data.get("presentation")
                    result_summary = ev.data.get("reason") or ev.data.get("content", "")[:120]
                    if ev.data.get("approval_required") and isinstance(presentation, dict):
                        result_summary = presentation.get("summary") or presentation.get("title") or result_summary
                    yield sse("tool_result", {
                        "id": ev.data.get("id", ""),
                        "error": bool(ev.data.get("error") or ev.data.get("blocked")),
                        "blocked": bool(ev.data.get("blocked")),
                        "preflight": bool(ev.data.get("preflight")),
                        "summary": _redact_secrets_in_text(str(result_summary))[:120],
                    })
                    if ev.data.get("approval_required"):
                        approval_payload = {
                            "run_id": run_id,
                            "approval_id": ev.data.get("approval_id", ""),
                            "tool": ev.data.get("tool") or ev.data.get("name") or "tool",
                            "level": ev.data.get("level", "execute"),
                            "reason": ev.data.get("reason", "Approval required"),
                            "arguments": ev.data.get("arguments") if isinstance(ev.data.get("arguments"), dict) else {},
                        }
                        risk = ev.data.get("risk")
                        if isinstance(risk, str) and risk:
                            approval_payload["risk"] = risk
                        if isinstance(presentation, dict):
                            approval_payload["presentation"] = presentation
                        yield sse("approval_required", approval_payload)
                elif t in ("skill_start", "skill_end"):
                    yield sse("skill", {"name": ev.data.get("skill", ""), "status": "start" if t == "skill_start" else ev.data.get("status", "end")})
                elif t == "route_decided":
                    yield sse("route_decided", {
                        "identity_id": str(ev.data.get("identity_id") or ""),
                        "identity_name": str(ev.data.get("identity_name") or ""),
                        "route_id": str(ev.data.get("route_id") or ""),
                        "pipeline_id": str(ev.data.get("pipeline_id") or ""),
                    })
                elif t == "gate_result":
                    payload: dict[str, object] = {
                        "skill": str(ev.data.get("skill") or ""),
                        "verdict": str(ev.data.get("verdict") or ""),
                        "reason": str(ev.data.get("reason") or ""),
                    }
                    if isinstance(ev.data.get("evidence_hash"), str):
                        payload["evidence_hash"] = ev.data["evidence_hash"]
                    yield sse("gate_result", payload)
                elif t == "gate_evidence":
                    evidence = ev.data.get("evidence")
                    yield sse("gate_evidence", {
                        "skill": str(ev.data.get("skill") or ""),
                        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
                        "evidence_hash": ev.data.get("evidence_hash"),
                    })
                elif t == "backtrack":
                    yield sse("backtrack", {
                        "from": str(ev.data.get("from") or ""),
                        "to": str(ev.data.get("to") or ""),
                        "reason": str(ev.data.get("reason") or ""),
                    })
                elif t == "awaiting_input":
                    yield sse("awaiting_input", {
                        "skill": str(ev.data.get("skill") or ""),
                        "reason": str(ev.data.get("reason") or "awaiting_user_input"),
                    })
                elif t == "blocked":
                    yield sse("message", {"text": f"\n⛔ 已阻断：{ev.data.get('reason', '')}\n"})
                elif t == "token_usage":
                    if self.token_stats_service is not None:
                        try:
                            project_path = str(working_dir or "")
                            await self.token_stats_service.record_usage(
                                session_id=session_id,
                                run_id=run_id,
                                project_name=Path(project_path).name if project_path else "",
                                project_path=project_path,
                                model=model_name,
                                usage=ev.data,
                            )
                        except Exception:
                            logger.warning(
                                "failed to persist token usage (session=%s)",
                                session_id,
                                exc_info=True,
                            )
                    yield sse("token_usage", {
                        "input_tokens": ev.data.get("input_tokens", 0),
                        "output_tokens": ev.data.get("output_tokens", 0),
                        "total_tokens": ev.data.get("total_tokens", 0),
                    })
                elif t == "context_usage":
                    yield sse("context_usage", {
                        "context_tokens": ev.data.get("context_tokens", 0),
                        "context_window": ev.data.get("context_window", CONTEXT_DISPLAY_WINDOW),
                        "context_percent": ev.data.get("context_percent", 0),
                        "estimated": bool(ev.data.get("estimated", True)),
                        "message_tokens": ev.data.get("message_tokens", 0),
                        "tool_schema_tokens": ev.data.get("tool_schema_tokens", 0),
                        "protocol_tokens": ev.data.get("protocol_tokens", 0),
                        "effective_context_window": ev.data.get(
                            "effective_context_window",
                            ev.data.get("context_window", CONTEXT_DISPLAY_WINDOW),
                        ),
                        "safe_input_budget": ev.data.get(
                            "safe_input_budget",
                            ev.data.get("context_window", CONTEXT_DISPLAY_WINDOW),
                        ),
                        "output_reserve": ev.data.get("output_reserve", 0),
                        "safety_margin": ev.data.get("safety_margin", 0),
                        "window_declared": bool(ev.data.get("window_declared", False)),
                        "output_limit_declared": bool(
                            ev.data.get("output_limit_declared", False)
                        ),
                        "fit_status": str(ev.data.get("fit_status", "unknown")),
                    })
                elif t == "context_compression_start":
                    yield sse("compression", {"active": True})
                elif t == "context_compression_end":
                    yield sse("compression", {"active": False})
                elif t == "incomplete":
                    terminal_status = "incomplete"
                    terminal_reason = str(ev.data.get("reason", "agent_incomplete"))
                elif t == "failed":
                    terminal_status = "failed"
                    terminal_reason = str(ev.data.get("reason", "agent_failed"))
                elif t == "run_finished":
                    status = ev.data.get("status")
                    if status in ("completed", "incomplete", "failed"):
                        terminal_status = status
                    reason = ev.data.get("reason")
                    terminal_reason = reason if isinstance(reason, str) and reason else None
                    if ev.data.get("memory_persist_failed"):
                        yield sse(
                            "message",
                            {
                                "text": (
                                    "\n⚠️ 本轮记忆保存失败，系统会在后续维护中重试。\n"
                                )
                            },
                        )
                # done is folded into the terminal SSE event below.
        except Exception:
            logger.exception("agent SSE execution failed (session=%s)", session_id)
            terminal_status = "failed"
            terminal_reason = "server_execution_error"
            terminal_notice = "执行失败（详情见服务端日志）"
        finally:
            # 客户端断连/引擎异常时生成器在 yield 处被终止，async for 之后的代码不会执行；
            # 落库放 finally 并用 shield 保护，请求被取消也能保住已生成的部分回复。
            close_run = getattr(run, "aclose", None)
            if callable(close_run):
                try:
                    await close_run()
                except Exception:
                    logger.warning("failed to close agent run (session=%s)", session_id, exc_info=True)
            reply_text = "".join(full_reply)
            if not reply_text:
                # Direct provider text and active provisional drafts have already
                # reached the client.  On disconnect preserve exactly that visible
                # state, but never persist drafts that were explicitly retracted.
                reply_text = "".join(
                    "".join(chunks) for chunks in visible_provisional_reply.values()
                ) or "".join(visible_raw_reply)
            if reply_text:
                try:
                    if _resume_run_id is not None and _message_id is not None:
                        # Only a resumed run that produced a replacement reply may
                        # delete the interrupted run's stale partial output; a
                        # resume that fails before producing text keeps it.
                        #
                        # discard MUST run before add_message: the discard deletes
                        # every assistant row between the user message and the next
                        # user turn, so inserting first would delete the new reply.
                        # Both steps run inside ONE shielded coroutine: two separate
                        # shields let a client disconnect (anyio re-delivers the
                        # cancellation on every loop tick) raise CancelledError —
                        # a BaseException the except-clause below cannot catch —
                        # at the await between them, deleting the old reply and
                        # never inserting the replacement.  As one unit the pair is
                        # atomic against that cancellation.
                        async def _persist_replacement() -> dict:
                            await self.session_repo.discard_assistant_messages_after_user(
                                session_id,
                                _message_id,
                            )
                            return await self.session_repo.add_message(
                                session_id, "assistant", reply_text
                            )

                        msg = await asyncio.shield(_persist_replacement())
                    else:
                        msg = await asyncio.shield(
                            self.session_repo.add_message(session_id, "assistant", reply_text)
                        )
                except Exception:
                    logger.exception("failed to persist streamed reply (session=%s)", session_id)
                    terminal_status = "failed"
                    terminal_reason = "reply_persistence_failed"
                    terminal_notice = "回复保存失败（详情见服务端日志）"

        if terminal_notice:
            yield sse("message", {"text": f"\n⚠️ {terminal_notice}\n"})
        done_payload = {
            "id": msg["id"] if msg else None,
            "status": terminal_status,
        }
        if run_id is not None:
            done_payload["run_id"] = run_id
        if terminal_reason:
            done_payload["reason"] = terminal_reason
        yield sse("done", done_payload)
