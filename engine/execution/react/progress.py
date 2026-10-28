"""Bounded progress observations, independent of execution or stopping policy."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProgressDecision:
    kind: Literal["progress", "repeat", "cycle", "failure"]
    count: int = 1
    period: int = 0
    failures: int = 0
    warn: bool = False


class ProgressLedger:
    def __init__(self, *, window_size: int = 30, repeat_threshold: int = 3) -> None:
        if repeat_threshold < 2 or window_size < max(15, repeat_threshold):
            raise ValueError("Window must cover three cycles of periods up to five")
        self._history: deque[str] = deque(maxlen=window_size)
        self._warned: set[tuple[str, ...]] = set()
        self._threshold = repeat_threshold
        self._last_failure: str | None = None
        self._failures = 0

    @property
    def history(self) -> tuple[str, ...]:
        return tuple(self._history)

    def reset_failures(self) -> None:
        self._last_failure = None
        self._failures = 0

    def break_sequence(self) -> None:
        self._history.clear()
        self._warned.clear()

    def record_failure(self, identity: str) -> int:
        self.break_sequence()
        # Retain a digest, not potentially sensitive tool output or arguments.
        fingerprint = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()
        self._failures = self._failures + 1 if fingerprint == self._last_failure else 1
        self._last_failure = fingerprint
        return self._failures

    def observe(self, tool_name: str, arguments: object, result_digest: str,
                *, is_error: bool = False) -> ProgressDecision:
        canonical = json.dumps([tool_name, arguments, result_digest], sort_keys=True,
                               ensure_ascii=False, default=str)
        fingerprint = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()
        if is_error:
            return ProgressDecision("failure", failures=self.record_failure(fingerprint))
        self.reset_failures()
        self._history.append(fingerprint)
        recent = self.history
        live = set(recent)
        self._warned = {pattern for pattern in self._warned if all(p in live for p in pattern[1:])}
        for period in range(2, 6):
            if len(recent) < period * 3:
                continue
            pattern = recent[-period:]
            if len(set(pattern)) > 1 and recent[-period * 3:] == pattern * 3:
                # Rotation-invariant identity avoids warning again at B,A,B,A.
                rotations = [pattern[i:] + pattern[:i] for i in range(period)]
                key = ("cycle", *min(rotations))
                warn = key not in self._warned
                self._warned.add(key)
                return ProgressDecision("cycle", count=3, period=period, warn=warn)
        count = recent.count(fingerprint)
        if count >= self._threshold:
            key = ("repeat", fingerprint)
            warn = key not in self._warned
            self._warned.add(key)
            return ProgressDecision("repeat", count=count, warn=warn)
        return ProgressDecision("progress", count=count)
