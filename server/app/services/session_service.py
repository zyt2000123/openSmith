from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import HTTPException

from engine.execution import (
    EngineRequest,
    RunStateError,
    RunStateStore,
    RunStatus,
    raw_text_delta,
    run_stream_with_runtime as engine_run_stream_with_runtime,
    resume_stream_with_runtime as engine_resume_stream_with_runtime,
)
from engine.context import CONTEXT_DISPLAY_WINDOW, summarize_session
from engine.identity import IdentityCatalog, IdentityCatalogError
from engine.llm.model_config import resolve_llm_config
from common.yaml_utils import YamlConfigError

from ..schemas.session import ContextCompressionOut, SessionOut, MessageOut
from ..infrastructure.repositories.session_repo import SessionRepo
from ..infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from .engine_runtime import build_engine_runtime, close_session_mcp_clients
from .token_stats_service import TokenStatsService

# Recent messages passed to the engine as short-term conversational context
_HISTORY_LIMIT = 10
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
            history = [{"role": "user", "content": f"[Session context summary]\n{summary}"}]
            if isinstance(cutoff, int) and cutoff > 0:
                rows = await self.session_repo.get_messages(session_id, offset=cutoff)
                rows = rows[-_HISTORY_LIMIT:]
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
        rows: list[dict],
        message_id: str,
    ) -> list[dict]:
        """Build bounded history preceding the exact user message being resumed."""
        user_index = next(
            (
                index for index, row in enumerate(rows)
                if row.get("id") == message_id and row.get("role") == "user"
            ),
            -1,
        )
        if user_index < 0:
            raise HTTPException(409, "Run session has no user message to resume")

        prior_rows = rows[:user_index]
        context_reader = getattr(self.session_repo, "get_context", None)
        context = await context_reader(session_id) if context_reader is not None else {}
        summary = context.get("context_summary") if isinstance(context, dict) else ""
        cutoff = context.get("context_summary_cutoff", 0) if isinstance(context, dict) else 0
        history: list[dict] = []
        if isinstance(summary, str) and summary:
            history.append({"role": "user", "content": f"[Session context summary]\n{summary}"})
            if isinstance(cutoff, int) and cutoff > 0:
                prior_rows = prior_rows[cutoff:]
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

    async def delete_session(self, agent_id: str, session_id: str) -> None:
        deleted = await self.session_repo.delete_owned(session_id, agent_id)
        if not deleted:
            raise HTTPException(404, "Session not found")
        await close_session_mcp_clients(session_id)

    async def compress_session(self, agent_id: str, session_id: str) -> ContextCompressionOut:
        session = await self.session_repo.get_owned(session_id, agent_id)
        if session is None:
            raise HTTPException(404, "Session not found")

        rows = await self.session_repo.get_messages(session_id)
        if not rows:
            raise HTTPException(400, "Cannot compress an empty session")

        profile = await self.agent_profile_repo.get(agent_id)
        profile_name = profile["name"] if profile else "Agent"
        _runtime, services = await self._build_runtime(agent_id, profile_name, session_id)
        try:
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
        rows = await self.session_repo.get_messages(state.session_id)
        user_message = next(
            (
                row for row in rows
                if row.get("id") == state.message_id and row.get("role") == "user"
            ),
            None,
        )
        if user_message is None or not isinstance(user_message.get("content"), str):
            raise HTTPException(409, "Run message is no longer available for resume")
        user_index = rows.index(user_message)
        if any(row.get("role") == "user" for row in rows[user_index + 1 :]):
            raise HTTPException(
                409,
                "Run has a newer user turn and cannot be resumed into this session safely",
            )
        history = await self._history_before_message(
            state.session_id,
            rows,
            state.message_id,
        )
        # This completes ownership and identity validation synchronously, before
        # recovery deletes any persisted partial assistant output.
        stream = await self.prepare_stream_message(
            agent_id,
            state.session_id,
            user_message["content"],
            skill_name=state.forced_skill,
            working_dir=state.working_dir,
            _history_override=history,
            _resume_run_id=run_id,
            _message_id=state.message_id,
            _execution_identity_id=state.identity_id,
        )
        await self.session_repo.discard_assistant_messages_after_user(
            state.session_id,
            state.message_id,
        )
        return stream

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
    ) -> AsyncGenerator[dict, None]:
        """Yield SSE event dicts. Streams text chunks as they arrive from the engine."""
        if _resume_run_id is None:
            # Fetch recent history BEFORE saving the new message (avoids duplication)
            history = await self._recent_history(session_id)
            user_message = await self.session_repo.add_message(session_id, "user", content)
            message_id = str(user_message["id"])
        else:
            history = list(_history_override or [])
            message_id = _message_id

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
            )
            if _resume_run_id is None:
                run = engine_run_stream_with_runtime(request, runtime, services)
            else:
                run = engine_resume_stream_with_runtime(
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
                elif t == "thinking":
                    yield sse("thinking", {"text": ev.data.get("text", ""), "done": bool(ev.data.get("done"))})
                elif t == "tool_call_start":
                    args = ev.data.get("arguments") or {}
                    hint = args.get("path") or args.get("file_path") or args.get("query") or args.get("command", "")
                    yield sse("tool_call", {"id": ev.data.get("id", ""), "name": ev.data.get("name", ""), "hint": str(hint)[:120]})
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
                        "summary": result_summary,
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
                    yield sse("gate_result", {
                        "skill": str(ev.data.get("skill") or ""),
                        "verdict": str(ev.data.get("verdict") or ""),
                        "reason": str(ev.data.get("reason") or ""),
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
