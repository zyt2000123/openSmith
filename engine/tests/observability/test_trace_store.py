from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engine.execution.events import EventType, ExecutionEvent
from engine.observability import TraceStore


def test_trace_store_persists_bounded_event_records(tmp_path):
    store = TraceStore(tmp_path)
    store.append(
        "run-1",
        ExecutionEvent(
            EventType.TOOL_CALL_RESULT,
            {"content": "x" * 10_000, "token": "secret-token"},
        ),
    )

    records = store.read("run-1")
    assert len(records) == 1
    assert records[0]["seq"] == 1
    assert records[0]["type"] == "tool_call_result"
    assert len(records[0]["data"]["content"]) <= 4096
    assert records[0]["data"]["token"] == "[REDACTED]"
    assert os.stat(tmp_path / "traces").st_mode & 0o777 == 0o700
    assert os.stat(tmp_path / "traces" / "run-1.jsonl").st_mode & 0o777 == 0o600


def test_trace_store_keeps_non_secret_token_metrics(tmp_path):
    store = TraceStore(tmp_path)
    store.append(
        "run-2",
        ExecutionEvent(
            EventType.TOKEN_USAGE,
            {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
        ),
    )

    record = store.read("run-2")[0]
    assert record["data"] == {
        "input_tokens": 100,
        "output_tokens": 25,
        "total_tokens": 125,
    }


def test_trace_store_reads_only_the_requested_tail(tmp_path):
    store = TraceStore(tmp_path)
    for index in range(5):
        store.append(
            "run-tail",
            ExecutionEvent(EventType.TEXT_DELTA, {"text": str(index)}),
        )

    records = store.read("run-tail", limit=2)

    assert [record["data"]["text"] for record in records] == ["3", "4"]


def test_trace_store_defers_sync_for_high_frequency_stream_events(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    sync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", sync_calls.append)
    store = TraceStore(tmp_path)

    store.append(
        "run-sync",
        ExecutionEvent(EventType.RAW_RESPONSE_EVENT, {"data": {"delta": "raw"}}),
    )
    store.append(
        "run-sync",
        ExecutionEvent(EventType.PROVISIONAL_TEXT_DELTA, {"text": "draft"}),
    )
    store.append("run-sync", ExecutionEvent(EventType.RUN_FINISHED, {}))

    assert len(sync_calls) == 1


def test_trace_store_recovers_sequence_without_reading_the_whole_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    TraceStore(tmp_path).append(
        "run-resume",
        ExecutionEvent(EventType.RUN_STARTED, {}),
    )

    def reject_read_text(_path: Path, *args, **kwargs):
        raise AssertionError("sequence recovery must stream the trace")

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    TraceStore(tmp_path).append(
        "run-resume",
        ExecutionEvent(EventType.RUN_FINISHED, {}),
    )

    with (tmp_path / "traces" / "run-resume.jsonl").open(encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle]
    assert [record["seq"] for record in records] == [1, 2]
