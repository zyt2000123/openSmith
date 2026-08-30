from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import engine_runtime
from engine.llm.client import ChatResponse
from engine.llm.model_config import LLMUsage
from engine.llm.replay import RecordingLLM, ReplayLLM, load_recording


_CONFIG = {"provider": "openai", "model": "test-model", "base_url": "https://example.invalid"}


class _EchoProvider:
    """Answers with the prompt it was given, so a turn names its own route."""

    stream = False
    model = "echo"

    async def chat(self, messages, tools=None, prefix_cache_key=None) -> ChatResponse:
        return ChatResponse(text=messages[-1]["content"])

    async def close(self) -> None:
        pass


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> object:
    """Replace the real client factory — this test is about the wrapper only."""
    sentinel = object()
    monkeypatch.setattr(engine_runtime, "build_llm_client", lambda config: sentinel)
    return sentinel


def test_recording_stays_off_without_the_env_var(
    monkeypatch: pytest.MonkeyPatch, stub_client: object
) -> None:
    monkeypatch.delenv("AGENT_SMITH_RECORD_LLM", raising=False)

    client = engine_runtime.LLMClientManager().get_for_config(dict(_CONFIG))

    assert client is stub_client


def test_recording_wraps_the_client_and_creates_the_target_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_client: object
) -> None:
    target = tmp_path / "recordings" / "case.jsonl"
    monkeypatch.setenv("AGENT_SMITH_RECORD_LLM", str(target))

    client = engine_runtime.LLMClientManager().get_for_config(dict(_CONFIG))

    assert isinstance(client, RecordingLLM)
    assert target.parent.is_dir(), "recorder must create the directory it writes into"


def test_recording_blank_env_var_is_treated_as_off(
    monkeypatch: pytest.MonkeyPatch, stub_client: object
) -> None:
    monkeypatch.setenv("AGENT_SMITH_RECORD_LLM", "   ")

    client = engine_runtime.LLMClientManager().get_for_config(dict(_CONFIG))

    assert client is stub_client


def test_gate_turns_do_not_interleave_into_the_chat_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """回放按位置供轮:门禁的轮混进主录制,主循环第 2 轮就会拿到门禁的回答。

    interactive 与 gate 常常解析成同一条路线(同一份 config),此时两者共用一个
    缓存客户端 —— 分流必须撑得住这种复用。
    """
    target = tmp_path / "case.jsonl"
    monkeypatch.setenv("AGENT_SMITH_RECORD_LLM", str(target))
    monkeypatch.setattr(engine_runtime, "resolve_llm_config", lambda *, usage: dict(_CONFIG))
    monkeypatch.setattr(engine_runtime, "build_llm_client", lambda config: _EchoProvider())
    manager = engine_runtime.LLMClientManager()

    chat = manager.get(LLMUsage.INTERACTIVE)
    gate = manager.get(LLMUsage.GATE)
    asyncio.run(chat.chat([{"role": "user", "content": "chat-1"}]))
    asyncio.run(gate.chat([{"role": "user", "content": "gate-verdict"}]))
    asyncio.run(chat.chat([{"role": "user", "content": "chat-2"}]))

    replay = ReplayLLM(load_recording(target))
    replayed = [asyncio.run(replay.chat([])).text for _ in range(2)]

    assert replayed == ["chat-1", "chat-2"]
    assert [
        turn.response.text for turn in load_recording(tmp_path / "case.gate.jsonl")
    ] == ["gate-verdict"]
