from __future__ import annotations

from pathlib import Path

import pytest

from app.services import engine_runtime
from engine.llm.replay import RecordingLLM


_CONFIG = {"provider": "openai", "model": "test-model", "base_url": "https://example.invalid"}


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
