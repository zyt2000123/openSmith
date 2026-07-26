from __future__ import annotations

import os

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
