"""Native Anthropic Messages API adapter.

The adapter owns all Anthropic-specific concerns: authentication headers,
Messages request shape, tool/result conversion, and named SSE events.  The
rest of the engine receives the same ``ChatResponse`` and ``ProviderEvent``
contracts as it does from the OpenAI-compatible adapter.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..contracts import (
    ChatResponse,
    DEFAULT_CONTEXT_WINDOW,
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


_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STREAM_ERROR_TYPES = frozenset({
    "api_error",
    "overloaded_error",
    "rate_limit_error",
})


class _AnthropicStreamError(LLMResponseError):
    """Typed provider event error so pre-content retries remain bounded."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class _AnthropicStreamTruncatedError(LLMResponseError):
    """The provider dropped the stream before ``message_stop``.

    Kept distinct from other stream errors so the retry loop can treat a
    pre-content truncation as transient without retrying malformed payloads,
    mirroring the OpenAI adapter's ``_StreamTruncatedError``.
    """


class AnthropicAdapter(HTTPAdapterMixin):
    """Translate Anthropic Messages payloads and named SSE events."""

    provider = "anthropic"
    capabilities = ProviderCapabilities(
        streaming=True,
        tool_calls=True,
        prefix_cache_key=False,
    )
    _completion_path = "/v1/messages"
    _error_label = "Anthropic"

    def __init__(self, config: LLMProviderConfig) -> None:
        self.api_key = config.api_key
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.timeouts = config.timeouts
        self.context_window_declared = config.context_window is not None
        self.context_window = config.context_window or DEFAULT_CONTEXT_WINDOW
        self.max_output_tokens_declared = config.max_output_tokens is not None
        # Anthropic requires max_tokens; other adapters preserve their provider
        # defaults unless the shared configuration explicitly sets a limit.
        self.max_output_tokens = config.max_output_tokens or 4096
        self.thinking = config.thinking
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            timeout=self.timeouts.stream_timeout(),
            trust_env=False,
        )

    async def complete(self, request: LLMRequest) -> ChatResponse:
        payload = await self._request(self._request_body(request, stream=False))
        return self._parse_response(payload)

    def stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
        return self._stream_response(request)

    async def _stream_response(self, request: LLMRequest) -> AsyncIterator[ProviderEvent]:
        body = self._request_body(request, stream=True)

        for attempt in range(MAX_RETRIES):
            http_request = self._http.build_request("POST", self._completion_path, json=body)
            response: httpx.Response | None = None
            retry_after: float | None = None
            saw_content_event = False
            saw_stop = False
            raw_finish_reason: str | None = None
            usage: dict[str, Any] = {}
            emitted_usage = False
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

                async for event_name, payload_text in self._iter_sse(response):
                    try:
                        event = json.loads(payload_text)
                    except (json.JSONDecodeError, RecursionError) as exc:
                        raise LLMResponseError("Anthropic stream contains invalid JSON.") from exc
                    if not isinstance(event, dict):
                        raise LLMResponseError("Anthropic stream event must be a JSON object.")

                    event_type = event_name or event.get("type")
                    if not isinstance(event_type, str):
                        continue

                    if event_type == "message_start":
                        message = event.get("message")
                        if isinstance(message, dict):
                            self._merge_usage(usage, message.get("usage"))
                            message_model = message.get("model")
                            if isinstance(message_model, str) and message_model:
                                served_model = message_model
                        continue

                    if event_type == "content_block_start":
                        block = event.get("content_block")
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        index = event.get("index")
                        tool_id = block.get("id")
                        tool_name = block.get("name")
                        if not isinstance(tool_id, str) or not isinstance(tool_name, str):
                            raise LLMResponseError(
                                "Anthropic stream tool_use block is missing id or name."
                            )
                        for committed_event in commit_attempt():
                            yield committed_event
                        saw_content_event = True
                        yield ProviderEvent(
                            ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                            {
                                "index": index if isinstance(index, int) else 0,
                                "id": tool_id,
                                "name": tool_name,
                            },
                        )
                        continue

                    if event_type == "content_block_delta":
                        delta = event.get("delta")
                        if delta is None:
                            continue
                        if not isinstance(delta, dict):
                            raise LLMResponseError(
                                "Anthropic stream content_block_delta must be an object."
                            )
                        index = event.get("index")
                        if not isinstance(index, int):
                            index = 0
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = delta.get("text")
                            if isinstance(text, str) and text:
                                for committed_event in commit_attempt():
                                    yield committed_event
                                saw_content_event = True
                                yield ProviderEvent(
                                    ProviderEventType.OUTPUT_TEXT_DELTA,
                                    {"delta": text},
                                )
                        elif delta_type == "thinking_delta":
                            thinking = delta.get("thinking")
                            if isinstance(thinking, str) and thinking:
                                for committed_event in commit_attempt():
                                    yield committed_event
                                saw_content_event = True
                                yield ProviderEvent(
                                    ProviderEventType.REASONING_DELTA,
                                    {"delta": thinking},
                                )
                        elif delta_type == "input_json_delta":
                            partial_json = delta.get("partial_json")
                            if isinstance(partial_json, str):
                                for committed_event in commit_attempt():
                                    yield committed_event
                                saw_content_event = True
                                yield ProviderEvent(
                                    ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                                    {"index": index, "arguments_delta": partial_json},
                                )
                        continue

                    if event_type == "message_delta":
                        delta = event.get("delta")
                        if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                            raw_finish_reason = delta["stop_reason"]
                        self._merge_usage(usage, event.get("usage"))
                        if usage:
                            emitted_usage = True
                            usage_event = ProviderEvent(
                                ProviderEventType.USAGE,
                                {"usage": dict(usage)},
                            )
                            if created_emitted:
                                yield usage_event
                            else:
                                pending_events.append(usage_event)
                        continue

                    if event_type == "error":
                        error = event.get("error")
                        error_type = error.get("type") if isinstance(error, dict) else None
                        if not isinstance(error_type, str):
                            error_type = None
                        # Only the short error ``type`` — an enum-like
                        # identifier — is surfaced.  The free-text ``message``
                        # is dropped because relays echo request content
                        # (including the prompt) into it, and provider error
                        # bodies must not surface in exceptions or logs (see
                        # ``HTTPAdapterMixin._raise_for_status``).
                        raise _AnthropicStreamError(
                            f"Anthropic stream error: {error_type or 'unknown provider error'}",
                            retryable=error_type in _RETRYABLE_STREAM_ERROR_TYPES,
                        )

                    if event_type == "message_stop":
                        saw_stop = True
                        break
                    # ``ping`` and future unknown event types do not alter the
                    # normalized contract and are intentionally ignored.

                if not saw_stop:
                    raise _AnthropicStreamTruncatedError(
                        "Anthropic stream ended before message_stop."
                    )
                for committed_event in commit_attempt():
                    yield committed_event
                if usage and not emitted_usage:
                    yield ProviderEvent(ProviderEventType.USAGE, {"usage": dict(usage)})
                yield ProviderEvent(
                    ProviderEventType.RESPONSE_COMPLETED,
                    {
                        "finish_reason": normalize_finish_reason(raw_finish_reason),
                        "raw_finish_reason": raw_finish_reason,
                        "model": served_model or self.model,
                    },
                )
                return
            except _AnthropicStreamError as exc:
                if (
                    saw_content_event
                    or not exc.retryable
                    or attempt >= MAX_RETRIES - 1
                ):
                    raise
                logger.warning(
                    "Anthropic stream attempt %d returned a retryable error event, retrying",
                    attempt + 1,
                )
            except _AnthropicStreamTruncatedError as exc:
                # A relay that drops the connection before message_stop — with
                # no content emitted — is a transient failure: retry, mirroring
                # the pre-content retry behavior of the HTTP request path and
                # the OpenAI adapter.
                if saw_content_event or attempt >= MAX_RETRIES - 1:
                    raise
                logger.warning(
                    "Anthropic stream attempt %d ended before message_stop, retrying",
                    attempt + 1,
                )
            except httpx.HTTPStatusError as exc:
                if (
                    saw_content_event
                    or not is_retryable_status(exc.response.status_code)
                    or attempt >= MAX_RETRIES - 1
                ):
                    raise LLMResponseError(
                        f"Anthropic stream failed (HTTP {exc.response.status_code}) "
                        f"after {attempt + 1} attempt(s)"
                    ) from exc
                logger.warning(
                    "Anthropic stream attempt %d failed (HTTP %d), retrying",
                    attempt + 1, exc.response.status_code,
                )
                retry_after = retry_after_seconds(exc.response)
            except httpx.RequestError as exc:
                if saw_content_event or attempt >= MAX_RETRIES - 1:
                    raise LLMResponseError(
                        f"Anthropic stream failed after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                logger.warning(
                    "Anthropic stream attempt %d failed (%s), retrying",
                    attempt + 1, type(exc).__name__,
                )
            finally:
                if response is not None:
                    await response.aclose()

            await self._wait_for_retry(attempt, retry_after)

    def _request_body(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        system, messages = self._translate_messages(request.messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": self._with_cache_breakpoint(messages),
        }
        if system:
            # Anthropic caches by byte prefix in render order (tools → system →
            # messages), so one breakpoint on system covers the tool schemas as
            # well.  A ReAct run resends both unchanged on every iteration.
            # ponytail: the whole system prompt is one cached block, so a turn
            # whose retrieval layers changed re-writes it. Splitting system into
            # stable + retrieved blocks would hold the hit across turns too, but
            # needs the assembler's stable/volatile boundary plumbed down here.
            body["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if self.thinking:
            # Adaptive is the only on-mode on current Claude models: the model
            # decides per request how much to think.  Without this field those
            # models do not think at all, which left the thinking blocks this
            # adapter already parses unreachable.
            body["thinking"] = {"type": "adaptive"}
        if request.tools:
            body["tools"] = self._translate_tools(request.tools)
        if stream:
            body["stream"] = True
        return body

    @classmethod
    def _with_cache_breakpoint(
        cls,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Mark the newest content block so the conversation prefix is cached.

        The ReAct loop resends the whole tool-result history on every iteration;
        with only the system breakpoint the transcript would be re-billed each
        time.  The caller's list is left alone because the same history object is
        reused across requests.
        """
        if not messages:
            return messages
        last = dict(messages[-1])
        blocks = cls._content_blocks(last.get("content"))
        if not blocks:
            return messages
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = blocks
        return [*messages[:-1], last]

    @classmethod
    def _translate_messages(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Convert the engine's OpenAI-shaped conversation without mutation.

        Anthropic accepts top-level system text and alternating user/assistant
        turns.  System guidance added mid-conversation by the ReAct loop is
        retained as a user turn so its ordering remains meaningful.
        """
        system_parts: list[str] = []
        translated: list[dict[str, Any]] = []
        saw_non_system = False

        for message in messages:
            if not isinstance(message, dict):
                raise LLMResponseError("LLM request messages must be objects.")
            role = message.get("role")
            if not isinstance(role, str):
                raise LLMResponseError("LLM request message is missing a role.")

            if role == "system":
                text = cls._text_content(message.get("content"))
                if not saw_non_system:
                    if text:
                        system_parts.append(text)
                elif text:
                    cls._append_message(translated, "user", text)
                continue

            saw_non_system = True
            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise LLMResponseError("Tool result is missing tool_call_id.")
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": cls._text_content(message.get("content")),
                }
                cls._append_message(translated, "user", [tool_result])
                continue

            if role not in {"user", "assistant"}:
                raise LLMResponseError(f"Unsupported message role for Anthropic: {role!r}.")
            content = (
                cls._assistant_content(message)
                if role == "assistant"
                else cls._copy_content(message.get("content"))
            )
            cls._append_message(translated, role, content)

        if not translated:
            raise LLMResponseError("Anthropic requires at least one non-system message.")
        if translated[0]["role"] != "user":
            raise LLMResponseError("Anthropic conversations must begin with a user message.")
        return "\n\n".join(system_parts), translated

    @classmethod
    def _assistant_content(cls, message: dict[str, Any]) -> str | list[dict[str, Any]]:
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise LLMResponseError("Assistant tool_calls must be a list.")
        if not tool_calls:
            return cls._copy_content(message.get("content"))

        blocks = cls._content_blocks(message.get("content"))
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                raise LLMResponseError("Assistant tool call must be an object.")
            function = tool_call.get("function")
            if not isinstance(function, dict):
                raise LLMResponseError("Assistant tool call is missing function data.")
            tool_id = tool_call.get("id")
            name = function.get("name")
            if not isinstance(tool_id, str) or not isinstance(name, str):
                raise LLMResponseError("Assistant tool call is missing id or name.")
            raw_arguments = function.get("arguments", "{}")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise LLMResponseError(
                        "Assistant tool call contains invalid JSON arguments."
                    ) from exc
            else:
                arguments = raw_arguments
            if not isinstance(arguments, dict):
                raise LLMResponseError("Assistant tool call arguments must be an object.")
            blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": arguments,
            })
        return blocks

    @classmethod
    def _translate_tools(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        translated: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise LLMResponseError("Tool schema must be an object.")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise LLMResponseError("Only function tool schemas are supported by Anthropic.")
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise LLMResponseError("Tool schema is missing a function name.")
            schema = function.get("parameters")
            if schema is None:
                schema = {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                raise LLMResponseError("Tool function parameters must be a JSON object schema.")
            translated_tool: dict[str, Any] = {"name": name, "input_schema": schema}
            description = function.get("description")
            if isinstance(description, str) and description:
                translated_tool["description"] = description
            translated.append(translated_tool)
        return translated

    @classmethod
    def _append_message(
        cls,
        messages: list[dict[str, Any]],
        role: str,
        content: object,
    ) -> None:
        if messages and messages[-1]["role"] == role:
            merged = cls._merge_content(messages[-1]["content"], content)
            if role == "user":
                # Anthropic requires tool_result blocks to precede text in one
                # user turn.  The ReAct loop can interleave a system hint
                # between two tool results, and the merge would otherwise put
                # text in the middle of them — reorder so the results stay
                # grouped and leading.
                merged = cls._order_user_blocks(merged)
            messages[-1]["content"] = merged
            return
        messages.append({"role": role, "content": cls._copy_content(content)})

    @staticmethod
    def _order_user_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Order tool_result blocks first, preserving order within each group."""
        results = [block for block in blocks if block.get("type") == "tool_result"]
        rest = [block for block in blocks if block.get("type") != "tool_result"]
        return [*results, *rest]

    @classmethod
    def _merge_content(cls, left: object, right: object) -> list[dict[str, Any]]:
        return [*cls._content_blocks(left), *cls._content_blocks(right)]

    @staticmethod
    def _copy_content(content: object) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        return AnthropicAdapter._content_blocks(content)

    @staticmethod
    def _content_blocks(content: object) -> list[dict[str, Any]]:
        if content is None:
            return []
        if isinstance(content, str):
            if not content:
                return []
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            copied: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    raise LLMResponseError("Message content blocks must be objects.")
                copied.append(dict(block))
            return copied
        return [{"type": "text", "text": str(content)}]

    @staticmethod
    def _text_content(content: object) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "".join(text_parts)
        return str(content)

    def _parse_response(self, payload: object) -> ChatResponse:
        if not isinstance(payload, dict):
            raise LLMResponseError("Anthropic response must be a JSON object.")
        content = payload.get("content")
        if not isinstance(content, list):
            raise LLMResponseError("Anthropic response is missing content blocks.")

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallData] = []
        for block in content:
            if not isinstance(block, dict):
                raise LLMResponseError("Anthropic response content block must be an object.")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    reasoning_parts.append(thinking)
            elif block_type == "tool_use":
                tool_id = block.get("id")
                name = block.get("name")
                arguments = block.get("input")
                if not isinstance(tool_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                    raise LLMResponseError("Anthropic tool_use block is malformed.")
                tool_calls.append(ToolCallData(id=tool_id, name=name, arguments=arguments))

        raw_finish_reason = payload.get("stop_reason")
        usage = payload.get("usage")
        served_model = payload.get("model")
        return ChatResponse(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            usage=usage if isinstance(usage, dict) else None,
            finish_reason=normalize_finish_reason(raw_finish_reason),
            raw_finish_reason=raw_finish_reason if isinstance(raw_finish_reason, str) else None,
            model=served_model if isinstance(served_model, str) else "",
        )

    @staticmethod
    def _merge_usage(destination: dict[str, Any], source: object) -> None:
        if isinstance(source, dict):
            destination.update(source)

    @staticmethod
    async def _iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
        event_name: str | None = None
        data_lines: list[str] = []
        limiter = SSEStreamLimiter()
        async for line in response.aiter_lines():
            limiter.consume_line(line)
            if line == "":
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                    limiter.finish_event()
                event_name = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator:
                continue
            value = value.lstrip(" ")
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            yield event_name, "\n".join(data_lines)

    async def close(self) -> None:
        await self._http.aclose()
