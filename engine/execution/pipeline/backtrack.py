from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class FailureSignature:
    error_type: str
    context_hash: str


class FailureLoopGuard:
    """Per-node failure escalation: bounded retries, then one switch, then block.

    State is keyed by ``error_type`` (the failing node) alone. A global
    strategy set would let unrelated nodes' earlier failures block the
    current node prematurely, and keying counts by output hash never
    terminates because LLM output varies between retries.
    """

    def __init__(self, max_same: int = 2, max_attempts: int = 3) -> None:
        self.max_same = max_same
        self.max_attempts = max_attempts
        self._attempts: dict[str, int] = {}

    def record(self, sig: FailureSignature) -> Literal["retry", "switch", "blocked"]:
        attempts = self._attempts.get(sig.error_type, 0) + 1
        self._attempts[sig.error_type] = attempts

        if attempts < self.max_same:
            return "retry"
        if attempts < self.max_attempts:
            return "switch"
        return "blocked"
