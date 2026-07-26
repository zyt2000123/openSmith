"""Tests for generation-level LLM observability primitives."""

from __future__ import annotations

import asyncio

from engine.llm.observability import (
    GenerationRecord,
    current_generation_scope,
    current_purpose,
    emit_generation,
    generation_context,
    generation_sink,
    llm_purpose,
    set_default_generation_sink,
)


def _record(**overrides: object) -> GenerationRecord:
    defaults: dict = {
        "provider": "openai",
        "model": "test-model",
        "purpose": "main",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "ttft_ms": 5,
        "total_ms": 9,
        "stream": True,
        "ok": True,
    }
    defaults.update(overrides)
    return GenerationRecord(**defaults)


def test_purpose_defaults_to_other_and_nests_with_reset() -> None:
    assert current_purpose() == "other"
    with llm_purpose("main"):
        assert current_purpose() == "main"
        with llm_purpose("compact"):
            assert current_purpose() == "compact"
        assert current_purpose() == "main"
    assert current_purpose() == "other"


def test_generation_scope_propagates_and_resets() -> None:
    assert current_generation_scope() == (None, None)
    with generation_context(run_id="r1", session_id="s1"):
        assert current_generation_scope() == ("r1", "s1")
    assert current_generation_scope() == (None, None)


def test_emit_reaches_context_sink() -> None:
    seen: list[GenerationRecord] = []

    async def sink(record: GenerationRecord) -> None:
        seen.append(record)

    async def run() -> None:
        with generation_sink(sink):
            await emit_generation(_record())

    asyncio.run(run())
    assert len(seen) == 1
    assert seen[0].model == "test-model"


def test_emit_without_any_sink_is_a_noop() -> None:
    asyncio.run(emit_generation(_record()))


def test_emit_swallows_sink_failures() -> None:
    async def broken(record: GenerationRecord) -> None:
        raise RuntimeError("sink unavailable")

    async def run() -> None:
        with generation_sink(broken):
            await emit_generation(_record())

    asyncio.run(run())


def test_context_sink_wins_over_default_sink() -> None:
    default_seen: list[str] = []
    scoped_seen: list[str] = []

    async def default_sink(record: GenerationRecord) -> None:
        default_seen.append(record.model)

    async def scoped_sink(record: GenerationRecord) -> None:
        scoped_seen.append(record.model)

    set_default_generation_sink(default_sink)
    try:
        async def run() -> None:
            with generation_sink(scoped_sink):
                await emit_generation(_record())
            await emit_generation(_record(model="via-default"))

        asyncio.run(run())
    finally:
        set_default_generation_sink(None)

    assert scoped_seen == ["test-model"]
    assert default_seen == ["via-default"]


def test_records_carry_scope_and_unique_source_keys() -> None:
    with generation_context(run_id="r9", session_id="s9"):
        run_id, session_id = current_generation_scope()
    first = _record()
    second = _record()
    assert (run_id, session_id) == ("r9", "s9")
    assert first.source_key and second.source_key
    assert first.source_key != second.source_key
    assert first.occurred_at.endswith("+00:00")
