"""Adapter for the OpenAI Chat Completions protocol."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..contracts import (
    ChatResponse,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLMProviderConfig,
    LLMRequest,
    LLMResponseError,
    ProviderCapabilities,
    ToolCallData,
)
from ..events import ProviderEvent, ProviderEventType, normalize_finish_reason
from ._http import HTTPAdapterMixin, SSEStreamLimiter
from ._retry import MAX_RETRIES, is_retryable_status, retry_after_seconds

logger = logging.getLogger(__name__)


class OpenAIAdapter(HTTPAdapterMixin):
    """Translate OpenAI Chat Completions HTTP/SSE payloads into internal contracts."""

    provider = "openai"
    capabilities = ProviderCapabilities(
        streaming=True,
        tool_calls=True,
        prefix_cache_key=True,
    )
    _completion_path = "/chat/completions"
    _error_label = "LLM"

    def __init__(self, config: LLMProviderConfig) -> None:
        self.api_key = config.api_key
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.timeouts = config.timeouts
        self.max_output_tokens_declared = config.max_output_tokens is not None
        self.max_output_tokens = (
            config.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
        )
        self.context_window_declared = config.context_window is not None
        self.context_window = config.context_window or DEFAULT_CONTEXT_WINDOW
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeouts.stream_timeout(),
            trust_env=False,
        )

    async def complete(self, request: LLMRequest) -> ChatResponse:
        data = await self._request(self._request_body(request, stream=False))
        choice_data, choice = self._first_message_choice(data)

        tool_calls = [
            self._parse_tool_call(tool_call)
            for tool_call in choice.get("tool_calls") or []
        ]
        raw_finish_reason = choice_data.get("finish_reason")
        text = choice.get("content")
        if text is not None and not isinstance(text, str):
            raise LLMResponseError("LLM response content must be a string or null.")
        reasoning = choice.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise LLMResponseError("LLM response reasoning_content must be a string or null.")
        served_model = data.get("model")
        return ChatResponse(
            text=text or "",
            reasoning=reasoning or "",
            tool_calls=tool_calls,
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason if isinstance(raw_finish_reason, str) else None,
            model=served_model if isinstance(served_model, str) else "",
        )

    def stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
        return self._stream_response(request)

    async def _stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
        body = self._request_body(request, stream=True)

        for attempt in range(MAX_RETRIES):
            http_request = self._http.build_request("POST", self._completion_path, json=body)
            response: httpx.Response | None = None
            retry_after: float | None = None
            saw_content_event = False
            saw_done = False
            raw_finish_reason: str | None = None
            served_model: str | None = None
            created_emitted = False
            pending_events: list[ProviderEvent] = []

            def commit_attempt() -> list[ProviderEvent]:
                nonlocal created_emitted
                if created_emitted:
                    return []
                created_emitted = True
                committed = [
                    ProviderEvent(
                        ProviderEventType.RESPONSE_CREATED,
                        {"model": self.model},
                    ),
                    *pending_events,
                ]
                pending_events.clear()
                return committed

            try:
                response = await self._http.send(http_request, stream=True)
                await self._raise_for_status(response)
                limiter = SSEStreamLimiter()
                async for payload in self._iter_sse_payloads(response, limiter):
                    if payload.strip() == "[DONE]":
                        saw_done = True
                        break

                    try:
                        chunk = json.loads(payload)
                    except (json.JSONDecodeError, RecursionError) as exc:
                        raise LLMResponseError("Provider stream contains invalid JSON.") from exc
                    if not isinstance(chunk, dict):
                        raise LLMResponseError("Provider stream event must be a JSON object.")

                    chunk_model = chunk.get("model")
                    if served_model is None and isinstance(chunk_model, str) and chunk_model:
                        served_model = chunk_model

                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        usage_event = ProviderEvent(
                            ProviderEventType.USAGE,
                            {"usage": usage},
                        )
                        if created_emitted:
                            yield usage_event
                        else:
                            pending_events.append(usage_event)

                    choices = chunk.get("choices", [])
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    if delta is None:
                        delta = {}
                    if not isinstance(delta, dict):
                        raise LLMResponseError("Provider stream choice delta must be an object.")

                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        for committed_event in commit_attempt():
                            yield committed_event
                        saw_content_event = True
                        yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": text})

                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(reasoning, str) and reasoning:
                        for committed_event in commit_attempt():
                            yield committed_event
                        saw_content_event = True
                        yield ProviderEvent(ProviderEventType.REASONING_DELTA, {"delta": reasoning})

                    tool_calls = delta.get("tool_calls") or []
                    if not isinstance(tool_calls, list):
                        tool_calls = []
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue
                        function = tool_call.get("function")
                        if not isinstance(function, dict):
                            function = {}
                        index = tool_call.get("index", 0)
                        event_data: dict[str, Any] = {
                            "index": index if isinstance(index, int) else 0,
                        }
                        if isinstance(tool_call.get("id"), str):
                            event_data["id"] = tool_call["id"]
                        if isinstance(function.get("name"), str):
                            event_data["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            event_data["arguments_delta"] = function["arguments"]
                        if len(event_data) > 1:
                            for committed_event in commit_attempt():
                                yield committed_event
                            saw_content_event = True
                            yield ProviderEvent(
                                ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                                event_data,
                            )

                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str):
                        raw_finish_reason = finish_reason

                if not saw_done:
                    raise LLMResponseError("Provider stream ended before the [DONE] sentinel.")

                for committed_event in commit_attempt():
                    yield committed_event
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    {
                        "finish_reason": normalize_finish_reason(raw_finish_reason),
                        "raw_finish_reason": raw_finish_reason,
                        "model": served_model or self.model,
                    },
                )
                return
            except httpx.HTTPStatusError as exc:
                if (
                    saw_content_event
                    or not is_retryable_status(exc.response.status_code)
                    or attempt >= MAX_RETRIES - 1
                ):
                    raise LLMResponseError(
                        f"LLM stream failed (HTTP {exc.response.status_code}) "
                        f"after {attempt + 1} attempt(s)"
                    ) from exc
                logger.warning(
                    "LLM stream attempt %d failed (HTTP %d), retrying",
                    attempt + 1, exc.response.status_code,
                )
                retry_after = retry_after_seconds(exc.response)
            except httpx.RequestError as exc:
                if saw_content_event or attempt >= MAX_RETRIES - 1:
                    raise LLMResponseError(
                        f"LLM stream failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                logger.warning(
                    "LLM stream attempt %d failed (%s), retrying",
                    attempt + 1, type(exc).__name__,
                )
            finally:
                if response is not None:
                    await response.aclose()

            await self._wait_for_retry(attempt, retry_after)

    @staticmethod
    async def _iter_sse_payloads(
        response: httpx.Response,
        limiter: SSEStreamLimiter,
    ) -> AsyncIterator[str]:
        """Yield complete SSE ``data`` payloads and reset limits per event."""
        data_lines: list[str] = []
        has_event_fields = False

        async for line in response.aiter_lines():
            limiter.consume_line(line)
            if line == "":
                if not has_event_fields:
                    continue
                limiter.finish_event()
                if data_lines:
                    yield "\n".join(data_lines)
                data_lines = []
                has_event_fields = False
                continue

            has_event_fields = True
            field, separator, value = line.partition(":")
            if field != "data":
                continue
            if separator and value.startswith(" "):
                value = value[1:]
            data_lines.append(value)

        if has_event_fields:
            limiter.finish_event()
            if data_lines:
                yield "\n".join(data_lines)

    def _request_body(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": stream,
        }
        if request.tools:
            body["tools"] = request.tools
        body["max_tokens"] = self.max_output_tokens
        if request.prefix_cache_key:
            # This is intentionally adapter-specific: only compatible gateways
            # that understand ``extra_body`` receive this optimization hint.
            body["extra_body"] = {"prefix_cache_key": request.prefix_cache_key}
        return body

    @staticmethod
    def _first_message_choice(data: object) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(data, dict):
            raise LLMResponseError("LLM response must be a JSON object.")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMResponseError("LLM response is missing a valid first choice.")
        choice_data = choices[0]
        choice = choice_data.get("message")
        if not isinstance(choice, dict):
            raise LLMResponseError("LLM response choice is missing a message object.")
        return choice_data, choice

    @staticmethod
    def _parse_tool_call(tool_call: object) -> ToolCallData:
        if not isinstance(tool_call, dict):
            raise LLMResponseError("LLM response contains an invalid tool call.")
        tool_id = tool_call.get("id")
        function = tool_call.get("function")
        if not isinstance(tool_id, str) or not isinstance(function, dict):
            raise LLMResponseError("LLM response contains an invalid tool call.")
        name = function.get("name")
        if not isinstance(name, str):
            raise LLMResponseError("LLM response tool call is missing a function name.")

        raw_arguments = function.get("arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMResponseError(
                    "LLM response contains invalid JSON tool-call arguments."
                ) from exc
        else:
            arguments = raw_arguments
        if not isinstance(arguments, dict):
            raise LLMResponseError("LLM response tool-call arguments must be an object.")
        return ToolCallData(id=tool_id, name=name, arguments=arguments)

    async def close(self) -> None:
        await self._http.aclose()
