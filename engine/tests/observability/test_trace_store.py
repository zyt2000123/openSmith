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


def test_trace_store_keeps_cache_and_reasoning_token_metrics(tmp_path):
    """cache_read/write and reasoning tokens are numeric metrics, not secrets;
    they must persist as values in the trace (matching the summary projection)
    instead of being name-redacted to '[REDACTED]'."""
    store = TraceStore(tmp_path)
    store.append(
        "run-cache",
        ExecutionEvent(
            EventType.TOKEN_USAGE,
            {
                "input_tokens": 100,
                "output_tokens": 25,
                "total_tokens": 150,
                "cache_read_tokens": 60,
                "cache_write_tokens": 10,
                "reasoning_tokens": 40,
            },
        ),
    )

    record = store.read("run-cache")[0]
    assert record["data"]["cache_read_tokens"] == 60
    assert record["data"]["cache_write_tokens"] == 10
    assert record["data"]["reasoning_tokens"] == 40


@pytest.mark.parametrize(
    "credential",
    [
        "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        "glpat-ABCDEFGHIJKLMNOPQRSTUV",
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "Authorization: Basic b3BlbnNlc2FtZToxMjM0NTY3ODkw",
    ],
)
def test_trace_store_redacts_embedded_credentials_in_values(tmp_path, credential):
    """Credentials embedded inside an innocuous field (a shell command) must
    be redacted before persisting, for every common token family."""
    store = TraceStore(tmp_path)
    store.append(
        "run-secret",
        ExecutionEvent(
            EventType.TOOL_CALL_START,
            {"name": "shell", "command": f"curl -H 'Authorization: {credential}' example.com"},
        ),
    )

    record = store.read("run-secret")[0]
    assert "example.com" in record["data"]["command"]
    assert credential not in record["data"]["command"]
    assert "[REDACTED]" in record["data"]["command"]


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
    # The per-iteration progress records joined the deferred set: they used to
    # fsync one by one on the SSE delivery path, and they carry no resume or
    # approval decision.  seal() fsyncs the whole log before anchoring the head,
    # so a finished run stays durable without them.
    store.append("run-sync", ExecutionEvent(EventType.THINKING, {"text": "t"}))
    store.append("run-sync", ExecutionEvent(EventType.TOKEN_USAGE, {"total_tokens": 3}))
    store.append("run-sync", ExecutionEvent(EventType.CONTEXT_USAGE, {"used_tokens": 1}))
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


def test_trace_store_reads_only_records_appended_after_a_byte_cursor(tmp_path):
    store = TraceStore(tmp_path)
    store.append("run-cursor", ExecutionEvent(EventType.RUN_STARTED, {}))
    store.append(
        "run-cursor",
        ExecutionEvent(EventType.TOKEN_USAGE, {"total_tokens": 10}),
    )

    first_records, cursor = store.read_from("run-cursor")
    store.append(
        "run-cursor",
        ExecutionEvent(EventType.TOKEN_USAGE, {"total_tokens": 20}),
    )
    new_records, next_cursor = store.read_from("run-cursor", offset=cursor)

    assert [record["seq"] for record in first_records] == [1, 2]
    assert [record["seq"] for record in new_records] == [3]
    assert next_cursor > cursor


def test_trace_store_cursor_does_not_advance_past_an_incomplete_record(tmp_path):
    store = TraceStore(tmp_path)
    store.append("run-partial", ExecutionEvent(EventType.RUN_STARTED, {}))
    path = tmp_path / "traces" / "run-partial.jsonl"
    complete_size = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b'{"seq":2')

    first_records, cursor = store.read_from("run-partial")

    assert [record["seq"] for record in first_records] == [1]
    assert cursor == complete_size

    with path.open("ab") as handle:
        handle.write(b',"type":"token_usage","data":{"total_tokens":10}}\n')

    appended_records, next_cursor = store.read_from(
        "run-partial",
        offset=cursor,
    )

    assert [record["seq"] for record in appended_records] == [2]
    assert next_cursor == path.stat().st_size


def test_trace_store_only_chmods_when_creating_the_trace_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = TraceStore(tmp_path)
    store.append("run-chmod", ExecutionEvent(EventType.RUN_STARTED, {}))

    chmod_calls: list[Path] = []

    def record_chmod(path: Path, mode: int) -> None:
        chmod_calls.append(path)

    monkeypatch.setattr(Path, "chmod", record_chmod)
    store.append("run-chmod", ExecutionEvent(EventType.RUN_FINISHED, {}))

    # Appending to an already-private file must not re-chmod on every event.
    assert chmod_calls == []
    assert os.stat(tmp_path / "traces" / "run-chmod.jsonl").st_mode & 0o777 == 0o600
