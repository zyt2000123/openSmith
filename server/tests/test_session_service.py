from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import session_service as session_service_module
from app.services.session_service import SessionService
from engine.execution import RunStateStore
from engine.identity import IdentityCatalog


async def stream_prepared_message(service: SessionService, *args, **kwargs):
    """Consume the production preflight seam used by the HTTP router."""
    stream = await service.prepare_stream_message(*args, **kwargs)
    async for event in stream:
        yield event


async def resume_prepared_run(service: SessionService, *args, **kwargs):
    """Consume the production resume-preflight seam used by the HTTP router."""
    stream = await service.prepare_resume_run(*args, **kwargs)
    async for event in stream:
        yield event


class FakeSessionRepo:
    def __init__(self) -> None:
        self.saved_messages: list[tuple[str, str, str]] = []
        self.identity_id: str | None = None
        self.messages: list[dict] = []
        self.context_summary = ""
        self.context_summary_cutoff = 0
        self.deleted_sessions: list[tuple[str, str]] = []

    async def exists(self, session_id: str, agent_id: str) -> bool:
        return True

    async def get_owned(self, session_id: str, agent_id: str) -> dict | None:
        return {
            "id": session_id,
            "agent_id": agent_id,
            "identity_id": self.identity_id,
            "title": "Test session",
            "created_at": "2026-07-07T00:00:00Z",
            "model_profile": None,
        }

    async def claim_identity(self, session_id: str, agent_id: str, identity_id: str) -> bool:
        if self.identity_id is not None:
            return False
        self.identity_id = identity_id
        return True

    async def get_messages(self, session_id: str, limit: int = 0, offset: int = 0) -> list[dict]:
        return self.messages[offset:] if limit == 0 else self.messages[offset : offset + limit]

    async def get_message(self, session_id: str, message_id: str) -> dict | None:
        return next(
            (m for m in self.messages if m["id"] == message_id),
            None,
        )

    async def get_messages_since(
        self, session_id: str, message_id: str, limit: int = 20
    ) -> list[dict]:
        try:
            start = next(
                index for index, m in enumerate(self.messages) if m["id"] == message_id
            )
        except StopIteration:
            return []
        return self.messages[start + 1 : start + 1 + limit]

    async def get_messages_before(
        self, session_id: str, message_id: str, limit: int
    ) -> list[dict]:
        try:
            end = next(
                index for index, m in enumerate(self.messages) if m["id"] == message_id
            )
        except StopIteration:
            return []
        return self.messages[max(0, end - limit) : end]

    async def get_recent_messages(self, session_id: str, limit: int) -> list[dict]:
        return []

    async def get_context(self, session_id: str) -> dict:
        return {
            "context_summary": self.context_summary,
            "context_summary_cutoff": self.context_summary_cutoff,
        }

    async def set_context(self, session_id: str, summary: str, cutoff: int) -> None:
        self.context_summary = summary
        self.context_summary_cutoff = cutoff

    async def delete_owned(self, session_id: str, agent_id: str) -> bool:
        self.deleted_sessions.append((session_id, agent_id))
        return True

    async def add_message(self, session_id: str, role: str, content: str) -> dict:
        self.saved_messages.append((session_id, role, content))
        return {
            "id": f"{role}-1",
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": "2026-07-07T00:00:00Z",
        }

    async def discard_assistant_messages_after_user(self, session_id: str, user_message_id: str) -> int:
        target = next(
            (index for index, message in enumerate(self.messages) if message["id"] == user_message_id),
            -1,
        )
        if target < 0:
            return 0
        next_user = next(
            (
                index
                for index, message in enumerate(self.messages[target + 1 :], start=target + 1)
                if message["role"] == "user"
            ),
            len(self.messages),
        )
        before = len(self.messages)
        self.messages = self.messages[: target + 1] + [
            message
            for message in self.messages[target + 1 : next_user]
            if message["role"] != "assistant"
        ] + self.messages[next_user:]
        return before - len(self.messages)


class FakeAgentProfileRepo:
    async def get(self, agent_id: str) -> dict | None:
        return {"id": agent_id, "name": "Smith"}


@pytest.mark.asyncio
async def test_prepare_stream_message_validates_before_returning_a_generator(tmp_path: Path) -> None:
    class MissingSessionRepo(FakeSessionRepo):
        async def get_owned(self, session_id: str, agent_id: str) -> dict | None:
            return None

    service = SessionService(MissingSessionRepo(), FakeAgentProfileRepo(), identity_catalog=_identity_catalog(tmp_path))

    with pytest.raises(HTTPException) as exc_info:
        await service.prepare_stream_message("smith-id", "missing", "hello")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_messages_rejects_a_session_not_owned_by_the_agent() -> None:
    class ForeignSessionRepo(FakeSessionRepo):
        async def get_owned(self, session_id: str, agent_id: str) -> dict | None:
            return None

        async def get_messages(self, *args, **kwargs) -> list[dict]:
            raise AssertionError("foreign session messages must not be read")

    service = SessionService(ForeignSessionRepo(), FakeAgentProfileRepo())

    with pytest.raises(HTTPException) as exc_info:
        await service.list_messages("smith-id", "foreign-session")

    assert exc_info.value.status_code == 404


def _identity_catalog(tmp_path: Path) -> IdentityCatalog:
    (tmp_path / "smith.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: smith
name: Smith
default: true
routes: []
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "legal.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: legal
name: \u6cd5\u52a1\u52a9\u624b
routes:
  - id: contract_review
    keywords: [\u5408\u540c]
    pipeline: legal-contract-review
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "coding.yaml").write_text(
        """
schema: agentsmith.identity/v1
id: coding
name: Coding
routes: []
""".strip(),
        encoding="utf-8",
    )
    return IdentityCatalog.load(tmp_path)


class FakeRun:
    def __init__(self, events) -> None:
        self._events = events
        self.closed = False

    async def stream_events(self):
        async for event in self._events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


def _fake_run(factory):
    def build(request, runtime, services):
        return FakeRun(factory(request, runtime, services))

    return build


@pytest.mark.asyncio
async def test_stream_message_forwards_skill_name_and_blocked_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str | None] = {}

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        captured["forced_skill"] = request.forced_skill
        captured["session_id"] = runtime.session_id
        yield SimpleNamespace(type=SimpleNamespace(value="tool_call_result"), data={
            "id": "tool-1",
            "blocked": True,
            "reason": "permission denied",
        })
        yield SimpleNamespace(type=SimpleNamespace(value="tool_call_result"), data={
            "id": "tool-approval",
            "blocked": True,
            "approval_required": True,
            "approval_id": "approval-1",
                "name": "shell",
                "level": "execute",
                "reason": "Approval required for shell",
                "arguments": {"command": "git status"},
                "presentation": {
                    "title": "Run a shell command",
                    "summary": "Execute the requested command",
                    "details": [{"label": "Command", "value": "git status"}],
                    "reason": "This command may change files or system state.",
                },
        })
        yield SimpleNamespace(type=SimpleNamespace(value="tool_call_result"), data={
            "id": "tool-2",
            "preflight": True,
            "blocked": False,
            "error": False,
            "reason": "present facts and retry",
        })
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "done"})

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    svc = SessionService(FakeSessionRepo(), FakeAgentProfileRepo())
    events = [
        event
        async for event in stream_prepared_message(svc,
            "smith-id",
            "sess-1",
            "analyze this repo",
            skill_name="planning",
        )
    ]

    assert captured["forced_skill"] == "planning"
    assert captured["session_id"] == "sess-1"
    tool_events = [event for event in events if event["event"] == "tool_result"]
    blocked_payload = json.loads(tool_events[0]["data"])
    assert blocked_payload["blocked"] is True
    assert blocked_payload["preflight"] is False
    assert blocked_payload["error"] is True
    assert blocked_payload["summary"] == "permission denied"

    approval_tool_payload = json.loads(
        next(event for event in tool_events if json.loads(event["data"])["id"] == "tool-approval")["data"]
    )
    assert approval_tool_payload["summary"] == "Execute the requested command"

    approval_events = [event for event in events if event["event"] == "approval_required"]
    assert len(approval_events) == 1
    approval_payload = json.loads(approval_events[0]["data"])
    assert approval_payload == {
        "run_id": None,
        "approval_id": "approval-1",
            "tool": "shell",
            "level": "execute",
            "reason": "Approval required for shell",
            "arguments": {"command": "git status"},
            "presentation": {
                "title": "Run a shell command",
                "summary": "Execute the requested command",
                "details": [{"label": "Command", "value": "git status"}],
                "reason": "This command may change files or system state.",
            },
        }

    preflight_payload = json.loads(next(event for event in tool_events if json.loads(event["data"])["preflight"])["data"])
    assert preflight_payload["preflight"] is True
    assert preflight_payload["blocked"] is False
    assert preflight_payload["error"] is False
    assert preflight_payload["summary"] == "present facts and retry"


@pytest.mark.asyncio
async def test_stream_message_forwards_skillchain_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="route_decided"),
            data={
                "identity_id": "coding",
                "identity_name": "Coding",
                "route_id": "requirements-research",
                "pipeline_id": "requirements-research",
                "score": 1_000,
            },
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="gate_result"),
            data={"skill": "grilling", "verdict": "retry", "reason": "Need a target user."},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="backtrack"),
            data={"from": "research", "to": "grilling", "reason": "Scope is still ambiguous."},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="awaiting_input"),
            data={"skill": "grilling", "reason": "awaiting_user_input"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={"run_id": "run-1", "status": "incomplete", "reason": "awaiting_user_input"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "Help me shape this product idea",
        )
    ]

    lifecycle = {
        event["event"]: json.loads(event["data"])
        for event in events
        if event["event"] in {"route_decided", "gate_result", "backtrack", "awaiting_input"}
    }
    assert lifecycle == {
        "route_decided": {
            "identity_id": "coding",
            "identity_name": "Coding",
            "route_id": "requirements-research",
            "pipeline_id": "requirements-research",
        },
        "gate_result": {"skill": "grilling", "verdict": "retry", "reason": "Need a target user."},
        "backtrack": {"from": "research", "to": "grilling", "reason": "Scope is still ambiguous."},
        "awaiting_input": {"skill": "grilling", "reason": "awaiting_user_input"},
    }


@pytest.mark.asyncio
async def test_stream_message_persists_token_usage_with_project_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Recorder:
        async def record_usage(self, **kwargs):
            captured.update(kwargs)

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return (
            SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id),
            SimpleNamespace(llm=SimpleNamespace(model="gpt-test")),
        )

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_started"),
            data={"run_id": "run-1"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="token_usage"),
            data={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={"run_id": "run-1", "status": "completed"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(
            FakeSessionRepo(),
            FakeAgentProfileRepo(),
            token_stats_service=Recorder(),
        ),
            "smith-id",
            "sess-1",
            "hello",
            working_dir="/tmp/Agent-Smith",
        )
    ]

    assert captured == {
        "session_id": "sess-1",
        "run_id": "run-1",
        "project_name": "Agent-Smith",
        "project_path": "/tmp/Agent-Smith",
        "model": "gpt-test",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    assert json.loads(next(event for event in events if event["event"] == "token_usage")["data"]) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_stream_message_forwards_complete_context_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return (
            SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id),
            object(),
        )

    async def fake_engine_events(request, runtime, services):
        yield SimpleNamespace(type=SimpleNamespace(value="context_usage"), data={
            "context_tokens": 64_000,
            "context_window": 128_000,
            "context_percent": 58,
            "estimated": False,
            "message_tokens": 60_000,
            "tool_schema_tokens": 3_500,
            "protocol_tokens": 500,
            "effective_context_window": 128_000,
            "safe_input_budget": 110_000,
            "output_reserve": 4_096,
            "safety_margin": 13_904,
            "window_declared": True,
            "output_limit_declared": True,
            "fit_status": "fit",
        })
        yield SimpleNamespace(
            type=SimpleNamespace(value="text_delta"),
            data={"text": "done"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(
            FakeSessionRepo(),
            FakeAgentProfileRepo(),
        ), "smith-id", "sess-1", "hello")
    ]
    receipt = json.loads(
        next(event for event in events if event["event"] == "context_usage")["data"]
    )

    assert receipt["context_window"] == 128_000
    assert receipt["safe_input_budget"] == 110_000
    assert receipt["tool_schema_tokens"] == 3_500
    assert receipt["output_reserve"] == 4_096
    assert receipt["fit_status"] == "fit"


@pytest.mark.asyncio
async def test_resume_run_reuses_session_scope_and_discards_partial_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    repo = FakeSessionRepo()
    repo.identity_id = "smith"
    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    repo.messages = [
        {"id": "u-previous", "session_id": "sess-1", "role": "user", "content": "earlier", "created_at": "1"},
        {"id": "a-previous", "session_id": "sess-1", "role": "assistant", "content": "done", "created_at": "2"},
        {"id": "u-current", "session_id": "sess-1", "role": "user", "content": "continue audit", "created_at": "3"},
        {"id": "a-partial", "session_id": "sess-1", "role": "assistant", "content": "partial", "created_at": "4"},
    ]
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="sess-1",
        message_id="u-current",
        identity_id="coding",
        working_dir="/tmp/project",
        forced_skill="review",
    )
    store.transition("run-1", "running")
    store.transition("run-1", "incomplete")

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_resume_events(request, runtime, services, run_id):
        captured["request"] = request
        captured["runtime_session"] = runtime.session_id
        captured["run_id"] = run_id
        yield SimpleNamespace(type=SimpleNamespace(value="run_started"), data={"run_id": run_id})
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "resumed"})
        yield SimpleNamespace(type=SimpleNamespace(value="run_finished"), data={"status": "completed"})

    def fake_resume_stream(request, runtime, services, run_id):
        return FakeRun(fake_resume_events(request, runtime, services, run_id))

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_resume_stream_with_runtime",
        fake_resume_stream,
    )

    events = [
        event
        async for event in resume_prepared_run(SessionService(
            repo,
            FakeAgentProfileRepo(),
            identity_catalog=_identity_catalog(identities_dir),
            run_state_store=store,
        ), "smith-id", "run-1")
    ]

    request = captured["request"]
    assert request.message == "continue audit"
    assert request.history == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "done"},
    ]
    assert request.working_dir == "/tmp/project"
    assert request.forced_skill == "review"
    assert request.message_id == "u-current"
    assert request.identity_id == "smith"
    assert request.execution_identity_id == "coding"
    assert captured["runtime_session"] == "sess-1"
    assert captured["run_id"] == "run-1"
    assert [message["content"] for message in repo.messages] == ["earlier", "done", "continue audit"]
    assert repo.saved_messages[-1] == ("sess-1", "assistant", "resumed")
    assert json.loads(events[-1]["data"])["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_resume_run_rejects_an_older_run_without_deleting_later_turns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = FakeSessionRepo()
    repo.messages = [
        {"id": "u-1", "session_id": "sess-1", "role": "user", "content": "first", "created_at": "1"},
        {"id": "a-1", "session_id": "sess-1", "role": "assistant", "content": "partial", "created_at": "2"},
        {"id": "u-2", "session_id": "sess-1", "role": "user", "content": "later", "created_at": "3"},
        {"id": "a-2", "session_id": "sess-1", "role": "assistant", "content": "later reply", "created_at": "4"},
    ]
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="sess-1",
        message_id="u-1",
        identity_id="smith",
    )
    store.transition("run-1", "running")
    store.transition("run-1", "incomplete")
    with pytest.raises(HTTPException, match="newer user turn") as exc_info:
        _ = [
            event
            async for event in resume_prepared_run(SessionService(
                repo,
                FakeAgentProfileRepo(),
                run_state_store=store,
            ), "smith-id", "run-1")
        ]

    assert exc_info.value.status_code == 409
    assert [message["id"] for message in repo.messages] == ["u-1", "a-1", "u-2", "a-2"]


@pytest.mark.asyncio
async def test_prepare_resume_rejects_a_retired_identity_without_discarding_partial_reply(
    tmp_path: Path,
) -> None:
    """Resume preflight must be read-only until the identity is known to be valid."""
    repo = FakeSessionRepo()
    repo.identity_id = "smith"
    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    repo.messages = [
        {"id": "u-current", "session_id": "sess-1", "role": "user", "content": "continue audit", "created_at": "1"},
        {"id": "a-partial", "session_id": "sess-1", "role": "assistant", "content": "partial", "created_at": "2"},
    ]
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="sess-1",
        message_id="u-current",
        identity_id="retired",
    )
    store.transition("run-1", "running")
    store.transition("run-1", "incomplete")

    with pytest.raises(HTTPException) as exc_info:
        await SessionService(
            repo,
            FakeAgentProfileRepo(),
            identity_catalog=_identity_catalog(identities_dir),
            run_state_store=store,
        ).prepare_resume_run("smith-id", "run-1")

    assert exc_info.value.status_code == 422
    assert [message["id"] for message in repo.messages] == ["u-current", "a-partial"]


@pytest.mark.asyncio
async def test_resume_that_fails_before_text_preserves_the_partial_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A resume that never produces a replacement reply must not discard the
    interrupted run's persisted partial output."""
    repo = FakeSessionRepo()
    repo.identity_id = "smith"
    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    repo.messages = [
        {"id": "u-current", "session_id": "sess-1", "role": "user", "content": "continue audit", "created_at": "1"},
        {"id": "a-partial", "session_id": "sess-1", "role": "assistant", "content": "partial", "created_at": "2"},
    ]
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="sess-1",
        message_id="u-current",
        identity_id="coding",
        working_dir="/tmp/project",
        forced_skill="review",
    )
    store.transition("run-1", "running")
    store.transition("run-1", "incomplete")

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_resume_events(request, runtime, services, run_id):
        yield SimpleNamespace(type=SimpleNamespace(value="run_started"), data={"run_id": run_id})
        raise RuntimeError("engine exploded before any text")

    def fake_resume_stream(request, runtime, services, run_id):
        return FakeRun(fake_resume_events(request, runtime, services, run_id))

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_resume_stream_with_runtime",
        fake_resume_stream,
    )

    events = [
        event
        async for event in SessionService(
            repo,
            FakeAgentProfileRepo(),
            identity_catalog=_identity_catalog(identities_dir),
            run_state_store=store,
        ).resume_run("smith-id", "run-1")
    ]

    assert [message["id"] for message in repo.messages] == ["u-current", "a-partial"]
    assert repo.saved_messages == []
    done = json.loads(events[-1]["data"])
    assert done["status"] == "failed"


@pytest.mark.asyncio
async def test_resume_that_retracts_everything_preserves_the_partial_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even a resumed run whose provisional drafts are all retracted (so nothing
    survives to be persisted) must not delete the interrupted run's partials."""
    repo = FakeSessionRepo()
    repo.identity_id = "smith"
    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    repo.messages = [
        {"id": "u-current", "session_id": "sess-1", "role": "user", "content": "continue audit", "created_at": "1"},
        {"id": "a-partial", "session_id": "sess-1", "role": "assistant", "content": "partial", "created_at": "2"},
    ]
    store = RunStateStore(tmp_path)
    store.create(
        "run-1",
        agent_id="smith-id",
        session_id="sess-1",
        message_id="u-current",
        identity_id="smith",
    )
    store.transition("run-1", "running")
    store.transition("run-1", "incomplete")

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_resume_events(request, runtime, services, run_id):
        yield SimpleNamespace(type=SimpleNamespace(value="run_started"), data={"run_id": run_id})
        yield SimpleNamespace(type=SimpleNamespace(value="provisional_text_delta"), data={"provision_id": "draft-1", "text": "draft"})
        yield SimpleNamespace(type=SimpleNamespace(value="provisional_retract"), data={"provision_id": "draft-1", "reason": "withdrawn"})
        yield SimpleNamespace(type=SimpleNamespace(value="run_finished"), data={"status": "completed"})

    def fake_resume_stream(request, runtime, services, run_id):
        return FakeRun(fake_resume_events(request, runtime, services, run_id))

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_resume_stream_with_runtime",
        fake_resume_stream,
    )

    events = [
        event
        async for event in SessionService(
            repo,
            FakeAgentProfileRepo(),
            identity_catalog=_identity_catalog(identities_dir),
            run_state_store=store,
        ).resume_run("smith-id", "run-1")
    ]

    assert [message["id"] for message in repo.messages] == ["u-current", "a-partial"]
    assert repo.saved_messages == []
    done = json.loads(events[-1]["data"])
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_first_message_pins_default_react_identity_not_content_routed_identity(tmp_path: Path) -> None:
    repo = FakeSessionRepo()
    service = SessionService(
        repo,
        FakeAgentProfileRepo(),
        identity_catalog=_identity_catalog(tmp_path),
    )

    selected = await service._resolve_session_identity(
        "smith-id",
        "sess-1",
        "请审查这份合同",
        None,
    )
    follow_up = await service._resolve_session_identity(
        "smith-id",
        "sess-1",
        "顺便帮我整理一下措辞",
        None,
    )

    assert selected == "smith"
    assert follow_up == "smith"
    assert repo.identity_id == "smith"


@pytest.mark.asyncio
async def test_compress_session_persists_llm_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeSessionRepo()
    repo.messages = [
        {"id": "u1", "session_id": "sess-1", "role": "user", "content": "goal", "created_at": "1"},
        {"id": "a1", "session_id": "sess-1", "role": "assistant", "content": "done", "created_at": "2"},
        {"id": "u2", "session_id": "sess-1", "role": "user", "content": "next", "created_at": "3"},
        {"id": "a2", "session_id": "sess-1", "role": "assistant", "content": "answer", "created_at": "4"},
    ]

    summary = (
        "<context_summary>"
        "<conversation_overview>dense summary</conversation_overview>"
        "<key_knowledge>none</key_knowledge>"
        "<file_system_state>none</file_system_state>"
        "<recent_actions>none</recent_actions>"
        "<current_plan>none</current_plan>"
        "</context_summary>"
    )

    class FakeLlm:
        async def chat(self, messages):
            return SimpleNamespace(text=summary, finish_reason="stop")

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), SimpleNamespace(llm=FakeLlm())

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)

    result = await SessionService(repo, FakeAgentProfileRepo()).compress_session("smith-id", "sess-1")

    assert result.summary == summary
    assert result.message_count == 4
    assert repo.context_summary == result.summary
    assert repo.context_summary_cutoff == 4


@pytest.mark.asyncio
async def test_recent_history_uses_saved_summary_and_only_post_cutoff_messages() -> None:
    repo = FakeSessionRepo()
    repo.context_summary = "old work is complete"
    repo.context_summary_cutoff = 2
    repo.messages = [
        {"role": "user", "content": "old goal"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]

    history = await SessionService(repo, FakeAgentProfileRepo())._recent_history("sess-1")

    assert history == [
        {"role": "user", "content": "[Session context summary]\nold work is complete"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]


@pytest.mark.asyncio
async def test_delete_session_requires_owned_session_and_deletes_it() -> None:
    repo = FakeSessionRepo()
    service = SessionService(repo, FakeAgentProfileRepo())

    await service.delete_session("smith-id", "sess-1")

    assert repo.deleted_sessions == [("sess-1", "smith-id")]


@pytest.mark.asyncio
async def test_session_rejects_switching_a_pinned_identity(tmp_path: Path) -> None:
    repo = FakeSessionRepo()
    repo.identity_id = "legal"
    service = SessionService(
        repo,
        FakeAgentProfileRepo(),
        identity_catalog=_identity_catalog(tmp_path),
    )

    with pytest.raises(HTTPException) as exc:
        await service._resolve_session_identity(
            "smith-id",
            "sess-1",
            "hello",
            "smith",
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_stream_message_forwards_provider_text_delta_without_replaying_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="raw_response_event"),
            data={
                "type": "response.output_text.delta",
                "data": {"delta": "live reply"},
            },
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="text_delta"),
            data={"text": "live reply", "already_streamed": True},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={"run_id": "run-1", "status": "completed"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    repo = FakeSessionRepo()
    events = [
        event
        async for event in stream_prepared_message(SessionService(repo, FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    message_events = [event for event in events if event["event"] == "message"]
    assert [json.loads(event["data"])["text"] for event in message_events] == ["live reply"]
    assert ("sess-1", "assistant", "live reply") in repo.saved_messages
    assert json.loads(events[-1]["data"])["status"] == "completed"


@pytest.mark.asyncio
async def test_stream_message_surfaces_memory_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="text_delta"),
            data={"text": "done"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={
                "run_id": "run-1",
                "status": "completed",
                "memory_persist_failed": True,
            },
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    messages = [
        json.loads(event["data"])["text"]
        for event in events
        if event["event"] == "message"
    ]
    assert any("记忆" in message and "失败" in message for message in messages)


@pytest.mark.asyncio
async def test_stream_message_forwards_provisional_lifecycle_and_persists_only_committed_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="raw_response_event"),
            data={
                "type": "response.output_text.delta",
                "data": {"delta": "discard me"},
                "provision_id": "draft-1",
            },
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="provisional_text_delta"),
            data={"provision_id": "draft-1", "text": "discard me"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="provisional_retract"),
            data={"provision_id": "draft-1", "reason": "incomplete_final_repair"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="provisional_text_delta"),
            data={"provision_id": "draft-2", "text": "final answer"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="provisional_commit"),
            data={"provision_id": "draft-2"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="text_delta"),
            data={"text": "final answer", "already_streamed": True},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={"run_id": "run-1", "status": "completed"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    repo = FakeSessionRepo()
    events = [
        event
        async for event in stream_prepared_message(SessionService(repo, FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    lifecycle = [event for event in events if event["event"].startswith("provisional")]
    assert [event["event"] for event in lifecycle] == [
        "provisional_text_delta",
        "provisional_retract",
        "provisional_text_delta",
        "provisional_commit",
    ]
    assert [json.loads(event["data"]) for event in lifecycle] == [
        {"provision_id": "draft-1", "text": "discard me"},
        {"provision_id": "draft-1", "reason": "incomplete_final_repair"},
        {"provision_id": "draft-2", "text": "final answer"},
        {"provision_id": "draft-2"},
    ]
    assert not [event for event in events if event["event"] == "message"]
    assert ("sess-1", "assistant", "final answer") in repo.saved_messages


@pytest.mark.asyncio
async def test_stream_message_forwards_validated_smith_ui_event(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="smith_ui"),
            data={
                "version": 1,
                "spec": {
                    "root": "summary",
                    "elements": {
                        "summary": {
                            "type": "Heading",
                            "props": {"text": "Deployment", "level": "h1"},
                            "children": [],
                        }
                    },
                },
                "images": [],
            },
        )
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "Shown above."})
        yield SimpleNamespace(type=SimpleNamespace(value="run_finished"), data={"status": "completed"})

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "show deployment",
        )
    ]

    ui = [event for event in events if event["event"] == "smith_ui"]
    assert len(ui) == 1
    assert json.loads(ui[0]["data"])["spec"]["root"] == "summary"


@pytest.mark.asyncio
async def test_stream_message_forwards_engine_smith_ui_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="smith_ui_fallback"),
            data={
                "reason": "component type 'TextInput' is not permitted",
                "code": '{"type":"TextInput"}',
            },
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "show deployment",
        )
    ]

    fallback = [event for event in events if event["event"] == "smith_ui_fallback"]
    assert len(fallback) == 1
    payload = json.loads(fallback[0]["data"])
    assert "not permitted" in payload["reason"]
    assert '"TextInput"' in payload["code"]


@pytest.mark.asyncio
async def test_stream_message_saves_visible_provisional_reply_on_client_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="raw_response_event"),
            data={
                "type": "response.output_text.delta",
                "data": {"delta": "partial reply"},
                "provision_id": "draft-1",
            },
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="provisional_text_delta"),
            data={"provision_id": "draft-1", "text": "partial reply"},
        )
        yield SimpleNamespace(
            type=SimpleNamespace(value="raw_response_event"),
            data={
                "type": "response.output_text.delta",
                "data": {"delta": " never sent"},
                "provision_id": "draft-1",
            },
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    captured: dict[str, FakeRun] = {}

    def fake_run(request, runtime, services) -> FakeRun:
        run = FakeRun(fake_engine_reply_events(request, runtime, services))
        captured["run"] = run
        return run

    monkeypatch.setattr(session_service_module, "engine_run_stream_with_runtime", fake_run)

    repo = FakeSessionRepo()
    svc = SessionService(repo, FakeAgentProfileRepo())
    stream = await svc.prepare_stream_message("smith-id", "sess-1", "hello")
    await anext(stream)
    await anext(stream)
    # 模拟客户端断连：SSE 响应会 aclose 生成器，触发 GeneratorExit
    await stream.aclose()

    assert ("sess-1", "assistant", "partial reply") in repo.saved_messages
    assert captured["run"].closed is True


@pytest.mark.asyncio
async def test_stream_message_marks_model_output_limit_as_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "partial answer"})
        yield SimpleNamespace(
            type=SimpleNamespace(value="incomplete"),
            data={"reason": "model_output_limit", "continuations": 2},
        )
        yield SimpleNamespace(type=SimpleNamespace(value="done"), data={})

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    done = json.loads(events[-1]["data"])
    assert done == {
        "id": "assistant-1",
        "status": "incomplete",
        "reason": "model_output_limit",
    }


@pytest.mark.asyncio
async def test_stream_message_preserves_a_blocked_run_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(
            type=SimpleNamespace(value="run_finished"),
            data={"run_id": "run-1", "status": "incomplete", "reason": "blocked"},
        )

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    assert json.loads(events[-1]["data"]) == {
        "id": None,
        "status": "incomplete",
        "reason": "blocked",
    }


@pytest.mark.asyncio
async def test_stream_message_marks_unhandled_engine_error_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        if False:
            yield None
        raise RuntimeError("unexpected engine failure")

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FakeSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    assert any(event["event"] == "message" for event in events)
    done = json.loads(events[-1]["data"])
    assert done == {"id": None, "status": "failed", "reason": "server_execution_error"}


@pytest.mark.asyncio
async def test_stream_message_persists_visible_reply_when_the_consumer_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 SSE 断连是消费方协程被取消，不是 aclose()。

    sse_starlette 在未配置 send_timeout 时不会显式 aclose body_iterator，断连由
    task group 的 cancel scope 注入 CancelledError。上面那条 aclose() 测试走的是
    GeneratorExit 路径；这条复现取消路径，锁定 finally + asyncio.shield 在取消下
    同样保住已经发给客户端的回复。
    """

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    streaming = asyncio.Event()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "visible so far"})
        streaming.set()
        await asyncio.sleep(3600)  # 停在引擎等待点，等取消到达
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": " never sent"})

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    # 落库要挂起并让测试知道自己进来了，才能在 cleanup 期间投第二次取消 ——
    # Task.cancel() 只递送一次，单次取消下 finally 里的 await 本来就跑得完，
    # asyncio.shield 真正防的是 cleanup 还在 await 时再次被取消。
    persisting = asyncio.Event()

    class SuspendingSessionRepo(FakeSessionRepo):
        async def add_message(self, session_id: str, role: str, content: str) -> dict:
            if role == "assistant":
                persisting.set()
                await asyncio.sleep(0.05)
            return await super().add_message(session_id, role, content)

    repo = SuspendingSessionRepo()
    service = SessionService(repo, FakeAgentProfileRepo())

    async def consume() -> None:
        async for _ in stream_prepared_message(service, "smith-id", "sess-1", "hello"):
            pass

    task = asyncio.create_task(consume())
    await streaming.wait()
    task.cancel()  # 断连：中断事件流，进入 finally
    await persisting.wait()
    task.cancel()  # cleanup 期间再次施压：没有 shield 就会丢掉这条回复
    with pytest.raises(asyncio.CancelledError):
        await task

    # shield 让 inner 协程在 task 已经抛出之后继续跑完，所以要等它落完库再断言。
    await asyncio.sleep(0.1)
    assert ("sess-1", "assistant", "visible so far") in repo.saved_messages


@pytest.mark.asyncio
async def test_stream_message_reports_failed_status_when_persisting_the_reply_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落库失败必须走成终态 failed + reply_persistence_failed，而不是静默完成。"""

    class FailingSessionRepo(FakeSessionRepo):
        async def add_message(self, session_id: str, role: str, content: str) -> dict:
            if role == "assistant":
                raise RuntimeError("database is locked")
            return await super().add_message(session_id, role, content)

    def fake_build_engine_runtime(agent_id: str, name: str, *, session_id: str | None = None):
        return SimpleNamespace(agent_id=agent_id, agent_name=name, session_id=session_id), object()

    async def fake_engine_reply_events(request, runtime, services):
        yield SimpleNamespace(type=SimpleNamespace(value="text_delta"), data={"text": "an answer"})

    monkeypatch.setattr(session_service_module, "build_engine_runtime", fake_build_engine_runtime)
    monkeypatch.setattr(
        session_service_module,
        "engine_run_stream_with_runtime",
        _fake_run(fake_engine_reply_events),
    )

    events = [
        event
        async for event in stream_prepared_message(SessionService(FailingSessionRepo(), FakeAgentProfileRepo()),
            "smith-id",
            "sess-1",
            "hello",
        )
    ]

    notices = [json.loads(event["data"])["text"] for event in events if event["event"] == "message"]
    assert any("回复保存失败" in notice for notice in notices)
    assert json.loads(events[-1]["data"]) == {
        "id": None,
        "status": "failed",
        "reason": "reply_persistence_failed",
    }
