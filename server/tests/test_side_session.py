"""Server integration tests for Module 3: /side sessions, isolation, redaction, and /promote."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.infrastructure.repositories.agent_profile_repo import AgentProfileRepo
from app.infrastructure.repositories.session_repo import SessionRepo
from app.main import app
from app.services.session_service import SessionService
from common import config as common_config
from common.database import close_db
from common.vault import VaultStore
from engine.execution import EventType, ExecutionEvent, RunStateStore
from engine.safety.taint import SessionTaintStore


@pytest_asyncio.fixture(autouse=True)
async def _close_db_after_test():
    yield
    await close_db()


def _auth_headers() -> dict[str, str]:
    from app.infrastructure.auth import get_local_token

    return {"Authorization": f"Bearer {get_local_token()}"}


@pytest.mark.asyncio
async def test_side_session_hidden_from_list_and_isolated_from_main_compress(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        main_resp = await client.post("/api/agent/sessions", json={"title": "Main Work"})
        assert main_resp.status_code == 201
        main_id = main_resp.json()["id"]

        repo = SessionRepo()
        await repo.add_message(main_id, "user", "Discuss main feature X")
        await repo.add_message(main_id, "assistant", "Implemented feature X")

        side_resp = await client.post(f"/api/agent/sessions/{main_id}/side", json={})
        assert side_resp.status_code == 200
        side = side_resp.json()
        side_id = side["id"]
        assert side["session_kind"] == "side"
        assert side["parent_session_id"] == main_id

        # Reusing active side returns the exact same side session
        side_again = await client.post(f"/api/agent/sessions/{main_id}/side", json={"reuse_existing": True})
        assert side_again.json()["id"] == side_id

        # GET /sessions excludes side sessions
        listed = await client.get("/api/agent/sessions")
        assert listed.status_code == 200
        listed_ids = [s["id"] for s in listed.json()]
        assert main_id in listed_ids
        assert side_id not in listed_ids

        # Add messages to side session
        await repo.add_message(side_id, "user", "SIDE_ONLY_QUESTION_ZZZ")
        await repo.add_message(side_id, "assistant", "SIDE_ONLY_ANSWER_ZZZ")

        # Compress main session — must NEVER see side session content
        captured_compress_inputs: list[list[dict]] = []

        async def fake_summarize(messages, llm, existing_summary=""):
            captured_compress_inputs.append(list(messages))
            return SimpleNamespace(usable=True, summary="Main summary only")

        monkeypatch.setattr("app.services.session_service.summarize_session", fake_summarize)
        monkeypatch.setattr(
            "app.services.session_service.SessionService._build_runtime",
            lambda self, a, p, s: asyncio.sleep(0, result=(SimpleNamespace(), SimpleNamespace(llm=object()))),
        )

        comp = await client.post(f"/api/agent/sessions/{main_id}/compress")
        assert comp.status_code == 200
        assert len(captured_compress_inputs) == 1
        serialized = str(captured_compress_inputs[0])
        assert "Discuss main feature X" in serialized
        assert "SIDE_ONLY_QUESTION_ZZZ" not in serialized
        assert "SIDE_ONLY_ANSWER_ZZZ" not in serialized


@pytest.mark.asyncio
async def test_side_stream_injects_redacted_snapshot_and_runs_concurrently_with_main(
    monkeypatch,
    tmp_path: Path,
):
    """Side stream receives ephemeral=True, read_only_tools=True, Vault-redacted snapshot, and does not block main."""
    vault = VaultStore(common_config.PATHS.data_dir / "vault.json")
    vault.set_secret("MY_PROD_TOKEN", "super-secret-vault-value-987654321", allowed_tools=["web_fetch"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        main_resp = await client.post("/api/agent/sessions", json={"title": "Main"})
        main_id = main_resp.json()["id"]
        agent_id = main_resp.json()["agent_id"]

        repo = SessionRepo()
        await repo.add_message(
            main_id,
            "user",
            "Check token super-secret-vault-value-987654321 and sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        )
        side_resp = await client.post(f"/api/agent/sessions/{main_id}/side", json={})
        side_id = side_resp.json()["id"]

    profile_repo = AgentProfileRepo()
    service = SessionService(repo, profile_repo, run_state_store=RunStateStore(common_config.PATHS.agent_dir))

    captured_requests: dict[str, object] = {}
    main_started = asyncio.Event()
    release_main = asyncio.Event()

    class _FakeStream:
        def __init__(self, run_id: str, session_id: str) -> None:
            self.run_id = run_id
            self._sid = session_id

        async def stream_events(self):
            yield ExecutionEvent(EventType.RUN_STARTED, {"run_id": self.run_id})
            if self._sid == main_id:
                main_started.set()
                await release_main.wait()
            yield ExecutionEvent(EventType.TEXT_DELTA, {"text": f"reply:{self._sid}"})
            yield ExecutionEvent(EventType.RUN_FINISHED, {"run_id": self.run_id, "status": "completed"})

        async def aclose(self):
            return None

    async def fake_build_runtime(self_svc, _agent_id, _profile_name, sid):
        return SimpleNamespace(session_id=sid), SimpleNamespace(llm=SimpleNamespace(model="m"))

    def fake_run_stream(request, runtime, services):
        captured_requests[runtime.session_id] = request
        return _FakeStream(f"run-{runtime.session_id}", runtime.session_id)

    monkeypatch.setattr(SessionService, "_build_runtime", fake_build_runtime)
    monkeypatch.setattr("app.services.session_service.engine_run_stream_with_runtime", fake_run_stream)

    # Start main stream (holds in-flight)
    main_gen = await service.prepare_stream_message(agent_id, main_id, "main work", working_dir=str(tmp_path))
    main_task = asyncio.create_task(_drain(main_gen))
    await asyncio.wait_for(main_started.wait(), timeout=2.0)

    # While main is running, side stream completes without blocking!
    side_gen = await service.prepare_stream_message(agent_id, side_id, "quick side question", working_dir=str(tmp_path))
    side_events = await asyncio.wait_for(_drain(side_gen), timeout=2.0)
    assert any(ev["event"] == "done" for ev in side_events)

    release_main.set()
    await asyncio.wait_for(main_task, timeout=2.0)

    side_req = captured_requests[side_id]
    main_req = captured_requests[main_id]
    assert main_req.ephemeral is False
    assert main_req.read_only_tools is False
    assert side_req.ephemeral is True
    assert side_req.read_only_tools is True

    # Snapshot was injected at head of history AND both Vault value + API key were redacted!
    snapshot_msg = side_req.history[0]["content"]
    assert snapshot_msg.startswith("[Read-only snapshot of main conversation]\n")
    assert "super-secret-vault-value-987654321" not in snapshot_msg
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in snapshot_msg


@pytest.mark.asyncio
async def test_promote_side_redacts_vault_propagates_taint_and_lists_in_sessions():
    vault = VaultStore(common_config.PATHS.data_dir / "vault.json")
    vault.set_secret("DB_PASS", "vault-db-password-secret-xyz", allowed_tools=["web_fetch"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        main_resp = await client.post("/api/agent/sessions", json={"title": "Parent"})
        main_id = main_resp.json()["id"]

        repo = SessionRepo()
        await repo.add_message(main_id, "user", "Parent saw vault-db-password-secret-xyz")

        # Mark parent session tainted
        parent_taint = SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=main_id)
        parent_taint.record("sensitive_file", "read_file", "file:.env")
        assert parent_taint.is_tainted is True

        side_resp = await client.post(f"/api/agent/sessions/{main_id}/side", json={})
        side_id = side_resp.json()["id"]
        await repo.add_message(side_id, "user", "Let's turn this into a real task")

        # H2: Side session inherits parent taint immediately on creation!
        side_taint_before = SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=side_id)
        assert side_taint_before.is_tainted is True
        assert "file:.env" in side_taint_before.labels()

        # Promote side -> main with keep_parent_context=True
        prom_resp = await client.post(
            f"/api/agent/sessions/{side_id}/promote",
            json={"title": "Promoted Task", "keep_parent_context": True},
        )
        assert prom_resp.status_code == 200
        promoted = prom_resp.json()
        assert promoted["session_kind"] == "main"
        assert promoted["title"] == "Promoted Task"

        # Now appears in GET /sessions
        listed = await client.get("/api/agent/sessions")
        assert side_id in [s["id"] for s in listed.json()]

        # Persisted context_summary redacted Vault secret
        ctx = await repo.get_context(side_id)
        assert "vault-db-password-secret-xyz" not in ctx["context_summary"]
        assert "Parent saw" in ctx["context_summary"]

        # Parent taint monotonically propagated so /promote cannot launder tainted data
        side_taint_after = SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=side_id)
        assert side_taint_after.is_tainted is True
        assert "file:.env" in side_taint_after.labels()


@pytest.mark.asyncio
async def test_side_inherits_late_parent_taint_on_run_and_promote_false_cannot_launder(
    monkeypatch,
    tmp_path: Path,
):
    """H2: Even if parent gets tainted AFTER side creation, running side inherits taint;
    promoting with keep_parent_context=False also merges parent taint."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        main_resp = await client.post("/api/agent/sessions", json={"title": "Clean Parent"})
        main_id = main_resp.json()["id"]
        agent_id = main_resp.json()["agent_id"]

        # Create side while parent is clean
        side_resp = await client.post(f"/api/agent/sessions/{main_id}/side", json={})
        side_id = side_resp.json()["id"]
        assert SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=side_id).is_tainted is False

        # Parent later becomes tainted
        parent_taint = SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=main_id)
        parent_taint.record("secret_output", "read_file", "secret:prod.env")

        # Running side MUST inherit parent taint before engine_run_stream_with_runtime runs
        repo = SessionRepo()
        service = SessionService(repo, AgentProfileRepo(), run_state_store=RunStateStore(common_config.PATHS.agent_dir))
        tainted_at_runtime: list[bool] = []

        class _StubStream:
            run_id = "run-side-taint"

            async def stream_events(self):
                yield ExecutionEvent(EventType.RUN_STARTED, {"run_id": self.run_id})
                yield ExecutionEvent(EventType.TEXT_DELTA, {"text": "ok"})
                yield ExecutionEvent(EventType.RUN_FINISHED, {"run_id": self.run_id, "status": "completed"})

            async def aclose(self):
                return None

        async def fake_build_runtime(self_svc, _a, _p, sid):
            tainted_at_runtime.append(
                SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=sid).is_tainted
            )
            return SimpleNamespace(session_id=sid), SimpleNamespace(llm=SimpleNamespace(model="m"))

        monkeypatch.setattr(SessionService, "_build_runtime", fake_build_runtime)
        monkeypatch.setattr("app.services.session_service.engine_run_stream_with_runtime", lambda r, rt, s: _StubStream())

        gen = await service.prepare_stream_message(agent_id, side_id, "fetch something", working_dir=str(tmp_path))
        _ = await _drain(gen)
        assert tainted_at_runtime == [True]

        # Now test a second side promoted with keep_parent_context=False directly after parent taint
        side2_resp = await client.post(
            f"/api/agent/sessions/{main_id}/side",
            json={"reuse_existing": False},
        )
        side2_id = side2_resp.json()["id"]
        # Even if side2's taint file were cleared before promote, promote(keep_parent_context=False) merges parent taint
        from engine.safety.taint import delete_session_taint
        delete_session_taint(common_config.PATHS.agent_dir, side2_id)
        prom = await client.post(
            f"/api/agent/sessions/{side2_id}/promote",
            json={"keep_parent_context": False},
        )
        assert prom.status_code == 200
        assert SessionTaintStore(state_dir=common_config.PATHS.agent_dir, session_id=side2_id).is_tainted is True


@pytest.mark.asyncio
async def test_delete_session_expected_kind_guards_main_from_side_close():
    """H1 server-side guard: DELETE ?expected_kind=side rejects deleting a main session with 409."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=_auth_headers()) as client:
        main_resp = await client.post("/api/agent/sessions", json={"title": "Precious Main"})
        main_id = main_resp.json()["id"]
        side_resp = await client.post(f"/api/agent/sessions/{main_id}/side", json={})
        side_id = side_resp.json()["id"]

        # Refused when trying to delete main as side
        bad_del = await client.delete(f"/api/agent/sessions/{main_id}?expected_kind=side")
        assert bad_del.status_code == 409

        # Main still exists
        listed = await client.get("/api/agent/sessions")
        assert main_id in [s["id"] for s in listed.json()]

        # Allowed when deleting actual side as side
        ok_del = await client.delete(f"/api/agent/sessions/{side_id}?expected_kind=side")
        assert ok_del.status_code == 204


async def _drain(gen):
    return [ev async for ev in gen]
