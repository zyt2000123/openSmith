"""Shared HTTP plumbing for provider adapters.

Provides the retry/backoff loop, the non-streaming JSON request cycle,
bounded response reading, and error-body extraction that every HTTP-based
adapter needs.  Adapters inherit from :class:`HTTPAdapterMixin` alongside
the ``ProviderAdapter`` protocol.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..contracts import LLMContextLengthError, LLMResponseError, LLMTimeouts
from ._retry import (
    MAX_RETRIES,
    is_retryable_status,
    retry_after_seconds,
    wait_for_retry,
)

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # 20 MiB cap on non-streaming responses
MAX_STREAM_TOTAL_BYTES = 20 * 1024 * 1024
MAX_STREAM_EVENT_BYTES = 1 * 1024 * 1024
MAX_STREAM_EVENTS = 10_000
MAX_STREAM_DURATION_SECONDS = 15 * 60
MAX_ERROR_BODY_BYTES = 64 * 1024

_CONTEXT_LIMIT_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context limit",
    "maximum context",
    "max context",
    "input length",
    "prompt is too long",
    "input is too long",
    "too many tokens",
)


def stream_event_budget(max_output_tokens: int | None) -> int:
    """Scale the event cap to the output the operator actually asked for.

    A fixed 10k events is a token-rate assumption, not a safety property -- the
    byte caps are the safety property.  A reasoning model configured for 32k
    output legitimately emits one event per token plus reasoning deltas, so the
    fixed cap killed the stream mid-answer: the error is not retryable once
    content has been seen, the already-rendered text is retracted, and the usage
    chunk at the tail is never read, so tokens that were spent record as zero.
    """
    if not max_output_tokens or max_output_tokens <= 0:
        return MAX_STREAM_EVENTS
    return max(MAX_STREAM_EVENTS, max_output_tokens * 2)


@dataclass
class SSEStreamLimiter:
    """Bound an untrusted provider SSE stream across every adapter."""

    max_events: int = MAX_STREAM_EVENTS
    max_duration_seconds: float = MAX_STREAM_DURATION_SECONDS
    total_bytes: int = 0
    event_bytes: int = 0
    events: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def consume_line(self, line: str) -> None:
        line_bytes = len(line.encode("utf-8"))
        self.total_bytes += line_bytes
        self.event_bytes += line_bytes
        if self.total_bytes > MAX_STREAM_TOTAL_BYTES:
            raise LLMResponseError("Provider stream exceeds the total byte limit.")
        if self.event_bytes > MAX_STREAM_EVENT_BYTES:
            raise LLMResponseError("Provider stream event exceeds the byte limit.")
        if time.monotonic() - self.started_at > self.max_duration_seconds:
            raise LLMResponseError("Provider stream exceeds the duration limit.")

    def finish_event(self) -> None:
        self.events += 1
        if self.events > self.max_events:
            raise LLMResponseError("Provider stream exceeds the event limit.")
        self.event_bytes = 0


class HTTPAdapterMixin:
    """Reusable HTTP helpers mixed into concrete provider adapters.

    Expects the concrete class to provide ``_http``, ``timeouts``, and
    ``_completion_path``, and to override ``_error_label`` for its own
    diagnostics prefix.
    """

    _http: httpx.AsyncClient  # provided by the concrete adapter
    timeouts: LLMTimeouts  # provided by the concrete adapter
    _completion_path: str  # POST endpoint for completion requests
    _error_label: str = "LLM"  # prefix used in diagnostics and log lines

    async def _wait_for_retry(self, attempt: int, retry_after: float | None = None) -> None:
        await wait_for_retry(attempt, retry_after)

    async def _retry_with_backoff(
        self,
        body: dict[str, Any],
        attempt: int,
        *,
        retry_after: float | None = None,
    ) -> dict[str, Any]:
        await self._wait_for_retry(attempt, retry_after)
        return await self._request(body, attempt + 1)

    async def _request(self, body: dict[str, Any], attempt: int = 0) -> dict[str, Any]:
        """POST ``body`` to ``_completion_path``, retrying transient failures."""
        try:
            raw = await self._read_bounded(
                "POST", self._completion_path, body, self.timeouts.request_timeout(),
            )
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise LLMResponseError(
                    f"{self._error_label} response contains invalid JSON."
                ) from exc
            if not isinstance(payload, dict):
                raise LLMResponseError(
                    f"{self._error_label} response must be a JSON object."
                )
            return payload
        except httpx.HTTPStatusError as exc:
            if is_retryable_status(exc.response.status_code) and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s request attempt %d failed (HTTP %d), retrying",
                    self._error_label, attempt + 1, exc.response.status_code,
                )
                return await self._retry_with_backoff(
                    body,
                    attempt,
                    retry_after=retry_after_seconds(exc.response),
                )
            raise LLMResponseError(
                f"{self._error_label} request failed (HTTP {exc.response.status_code}) "
                f"after {attempt + 1} attempt(s)."
            ) from exc
        except httpx.RequestError as exc:
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s request attempt %d failed (%s), retrying",
                    self._error_label, attempt + 1, type(exc).__name__,
                )
                return await self._retry_with_backoff(body, attempt)
            raise LLMResponseError(
                f"{self._error_label} request failed after {MAX_RETRIES} attempts: {exc}"
            ) from exc

    async def _read_error_text(self, response: httpx.Response) -> str:
        """Read only enough untrusted error data to classify the failure."""
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                remaining = MAX_ERROR_BODY_BYTES - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    break
        except Exception:
            return ""
        return " ".join(
            b"".join(chunks).decode("utf-8", errors="replace").split()
        )

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise a typed sanitized error before provider details leave the adapter."""
        if response.is_success:
            return
        detail = await self._read_error_text(response)
        logger.debug(
            "%s error body (HTTP %d): %s",
            self._error_label,
            response.status_code,
            detail[:500] + ("…" if len(detail) > 500 else ""),
        )
        context_error = self._context_length_error(response.status_code, detail)
        if context_error is not None:
            raise context_error
        response.raise_for_status()

    def _context_length_error(
        self,
        status_code: int,
        detail: str,
    ) -> LLMContextLengthError | None:
        if status_code not in {400, 413, 422}:
            return None
        folded = detail.casefold()
        if not any(marker in folded for marker in _CONTEXT_LIMIT_MARKERS):
            return None

        provider_code: str | None = None
        try:
            payload = json.loads(detail)
        except (json.JSONDecodeError, RecursionError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                raw_code = error.get("code") or error.get("type")
                if isinstance(raw_code, str) and raw_code:
                    provider_code = raw_code[:100]

        return LLMContextLengthError(
            f"{self._error_label} request exceeds the selected model context "
            f"window (HTTP {status_code}).",
            http_status=status_code,
            provider_code=provider_code,
        )

    async def _read_bounded(
        self,
        method: str,
        url: str,
        body: dict[str, Any],
        timeout: httpx.Timeout,
    ) -> bytes:
        """Stream-read with a hard byte cap -- aborts before buffering oversized bodies."""
        req = self._http.build_request(method, url, json=body, timeout=timeout)
        response = await self._http.send(req, stream=True)
        try:
            if not response.is_success:
                # stream=True 意味着响应体还没被读过，而 4xx 的排查成本几乎全在
                # provider 那段"哪个字段非法"里。但它不能进异常消息 —— 异常会外泄
                # 到日志与前端，而错误体常回显请求内容（含 prompt）。见
                # test_request_failure_does_not_surface_provider_error_body。
                # 折中：只落 debug 日志，排查时开 debug，默认零暴露。
                await self._raise_for_status(response)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise LLMResponseError(
                        f"Provider response exceeds {MAX_RESPONSE_BYTES} byte limit."
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            await response.aclose()
