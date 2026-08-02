"""Cryptographic evidence binding for gate verdicts.

A gate verdict is only trustworthy when it can be checked against the real
tool outputs it was computed from.  This module binds one tool execution to a
deterministic SHA-256 over ``(tool, call_id, arguments, content, is_error)``
— only the hash is ever stored, so no tool output leaks into the trace — and
binds an ordered list of such results into one ``evidence_hash`` that a
``GATE_RESULT`` event carries alongside its verdict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Deterministic JSON serialization for hashing (sorted keys, no spaces)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_result_hash(
    *,
    tool_name: str,
    call_id: str,
    arguments: dict,
    content: str,
    is_error: bool,
) -> str:
    """Deterministic binding of one tool result to its inputs and output."""
    payload = {
        "tool": tool_name,
        "call_id": call_id,
        "arguments": arguments,
        "content": content,
        "is_error": bool(is_error),
    }
    return sha256_hex(canonical_json(payload))


def evidence_hash_of(evidence: list[dict]) -> str:
    """SHA-256 over an ordered list of tool-result evidence entries."""
    return sha256_hex(canonical_json(evidence))
