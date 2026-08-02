"""Tamper-evidence: hash-chained audit and trace records detect any edit.

The audit log and per-run traces are now hash-chained JSONL.  Editing a
record, reordering records, deleting a middle record, or inserting a forged
record breaks the chain; a sealed anchor additionally detects a rollback to a
shorter-but-consistent chain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from common.hash_chain import HashChainLog, genesis_hash, record_hash, verify_chain
from engine.execution.events import EventType, ExecutionEvent
from engine.observability import TraceStore
from engine.safety.tool_guard import AuditLog, GuardResult


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rewrite(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _chain(tmp_path: Path, *, records: list[dict] | None = None) -> tuple[Path, HashChainLog]:
    path = tmp_path / "log.jsonl"
    chain = HashChainLog(path, namespace="test-ns")
    for record in (records or []):
        chain.append(record)
    return path, chain


def test_append_links_a_verifiable_chain(tmp_path: Path) -> None:
    path, chain = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    stored = _lines(path)
    assert [r["seq"] for r in stored] == [1, 2, 3]
    assert stored[0]["prev_hash"] == genesis_hash("test-ns")
    assert stored[1]["prev_hash"] == stored[0]["hash"]
    assert stored[2]["prev_hash"] == stored[1]["hash"]
    assert all(r["hash"] == record_hash(r) for r in stored)

    result = chain.verify()
    assert result.ok
    assert result.records == 3


def test_verify_passes_on_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"
    result = verify_chain(path, namespace="test-ns")
    assert result.ok
    assert result.records == 0


def test_editing_a_record_breaks_the_hash(tmp_path: Path) -> None:
    path, _ = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    records = _lines(path)
    records[1] = {**records[1], "data": "tampered"}
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "hash mismatch" in (result.failure or "")


def test_reordering_records_breaks_prev_hash(tmp_path: Path) -> None:
    """Reordering with renumbered, re-hashed records is caught by prev_hash.

    A tamperer who swaps two records, renumbers the seqs, and recomputes each
    record's own hash leaves the stale ``prev_hash`` links behind — the chain
    link that binds record N to record N-1's hash.
    """
    path, _ = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    records = _lines(path)
    records[1], records[2] = records[2], records[1]
    records[1]["seq"] = 2
    records[2]["seq"] = 3
    for record in records:
        record["hash"] = record_hash(record)
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "prev_hash mismatch" in (result.failure or "")


def test_deleting_a_middle_record_breaks_prev_hash(tmp_path: Path) -> None:
    """Deleting a record and renumbering the tail is caught by prev_hash."""
    path, _ = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    records = _lines(path)
    del records[1]
    records[1]["seq"] = 2
    for record in records:
        record["hash"] = record_hash(record)
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "prev_hash mismatch" in (result.failure or "")


def test_duplicate_seq_is_detected(tmp_path: Path) -> None:
    path, _ = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    records = _lines(path)
    records[2] = {**records[2], "seq": 2}
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "sequence gap" in (result.failure or "")


def test_unchained_record_after_chain_start_is_detected(tmp_path: Path) -> None:
    path, _ = _chain(tmp_path, records=[{"a": 1}, {"a": 2}])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"forged": True}) + "\n")

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "after the chain started" in (result.failure or "")


def test_legacy_file_links_into_a_verifiable_chain(tmp_path: Path) -> None:
    """A pre-chain JSONL tail is bound by the first chained record's prev_hash."""
    path = tmp_path / "legacy.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")

    chain = HashChainLog(path, namespace="test-ns")
    chain.append({"b": 3})
    chain.append({"b": 4})

    stored = _lines(path)
    assert stored[2]["seq"] == 1
    assert stored[2]["legacy_linked"] is True
    assert stored[2]["prev_hash"] == record_hash({"a": 2})
    assert stored[3]["prev_hash"] == stored[2]["hash"]

    result = chain.verify()
    assert result.ok
    assert result.records == 4


def test_editing_a_legacy_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    HashChainLog(path, namespace="test-ns").append({"b": 3})

    records = _lines(path)
    records[1] = {"a": "tampered"}
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    assert not result.ok
    assert "prev_hash mismatch" in (result.failure or "")


def test_rollback_after_seal_is_detected_by_anchor(tmp_path: Path) -> None:
    path, chain = _chain(tmp_path, records=[{"a": 1}, {"a": 2}, {"a": 3}])
    chain.seal()
    assert chain.anchor_path.is_file()

    # Roll back the log to a shorter-but-consistent chain.
    records = _lines(path)[:2]
    _rewrite(path, records)

    result = verify_chain(path, namespace="test-ns")
    # Inline chain is still self-consistent...
    assert result.ok
    # ...but the sealed anchor names the pre-rollback head.
    anchored = chain.verify()
    assert not anchored.ok
    assert "anchor mismatch" in (anchored.failure or "")


def test_audit_log_writes_a_verifiable_chain(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("shell", {"command": "pwd"}, GuardResult(allowed=True), call_id="c1", run_id="r1")
    audit.record("shell", {"command": "ls"}, GuardResult(allowed=True), call_id="c2", run_id="r1")
    # A different run seals the previous run's chain.
    audit.record("shell", {"command": "cat"}, GuardResult(allowed=True), call_id="c3", run_id="r2")
    audit.close()

    path = tmp_path / "audit.jsonl"
    records = _lines(path)
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert all("hash" in r and "prev_hash" in r for r in records)
    assert audit.verify().ok
    assert (tmp_path / "audit.jsonl.head").is_file()

    # Tamper with a middle record.
    records[1]["args_summary"] = {"command": "TAMPERED"}
    _rewrite(path, records)
    result = audit.verify()
    assert not result.ok
    assert "hash mismatch" in (result.failure or "")


def test_trace_store_writes_a_verifiable_chain(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    store.append("run-x", ExecutionEvent(EventType.RUN_STARTED, {}))
    store.append("run-x", ExecutionEvent(EventType.THINKING, {"text": "reasoning"}))
    store.seal("run-x")
    assert store.verify("run-x").ok

    path = tmp_path / "traces" / "run-x.jsonl"
    records = _lines(path)
    assert [r["seq"] for r in records] == [1, 2]
    assert all("hash" in r and "prev_hash" in r for r in records)
    assert (tmp_path / "traces" / "run-x.jsonl.head").is_file()

    # Tamper: rewrite one thinking record's text.
    records[1]["data"] = {"text": "altered"}
    _rewrite(path, records)
    result = store.verify("run-x")
    assert not result.ok
    assert "hash mismatch" in (result.failure or "")


def test_trace_store_chain_continues_across_instances(tmp_path: Path) -> None:
    """A fresh TraceStore re-derives the chain tail from disk (streaming)."""
    TraceStore(tmp_path).append("run-resume", ExecutionEvent(EventType.RUN_STARTED, {}))
    store = TraceStore(tmp_path)
    store.append("run-resume", ExecutionEvent(EventType.RUN_FINISHED, {}))
    assert store.verify("run-resume").ok
    records = _lines(tmp_path / "traces" / "run-resume.jsonl")
    assert [r["seq"] for r in records] == [1, 2]
    assert records[1]["prev_hash"] == records[0]["hash"]
