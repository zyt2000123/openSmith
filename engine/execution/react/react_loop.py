"""Canonical ReAct loop shared by text, stream, and skill execution paths."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import uuid4

from engine.context import ContextReceipt, fit_request, measure_request
from engine.context.compression import (
    compaction_policy_for_llm,
    trim_conversation_for_context_limit,
)
from engine.execution.evidence import tool_result_hash
from engine.execution.events import EventType, ExecutionEvent
from engine.execution.runtime_control import tool_blocked_prompt
from engine.llm.contracts import (
    ChatResponse,
    LLMContextLengthError,
    LLMResponseError,
    ToolCallData,
)
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.llm.usage import normalize_usage
from engine.safety.approval import (
    ApprovalRequest,
    ApprovalTimeoutError,
    build_approval_presentation,
    current_approval_context,
    summarize_arguments,
)
from engine.safety.fact_gate import FactGate, current_fact_gate
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall

from .budget import (
    CONTINUE_AFTER_LENGTH_HINT,
    CONVERSATION_HARD_LIMIT,
    CONVERSATION_KEEP_HEAD,
    CONVERSATION_KEEP_RECENT,
    DEFAULT_MAX_REACT_ITERS,
    INCOMPLETE_FINAL_AFTER_TOOL_HINT,
    MAX_FAILED_TOOL_RECOVERY_ITERS,
    MAX_IDENTICAL_TOOL_ERRORS,
    MAX_INCOMPLETE_FINAL_REPAIRS,
    MAX_LENGTH_CONTINUATIONS,
    MAX_PREFLIGHT_CHALLENGE_ITERS,
    PREFLIGHT_BUDGET_MESSAGE,
    TOOL_CALL_BUDGET_MESSAGE,
    TOOL_FAILURE_BUDGET_MESSAGE,
    TOOL_FAILURE_HINT,
    budget_exhausted_message,
    looks_like_incomplete_final_after_tool,
)
from .smith_ui import smith_ui_fallback, validate_smith_ui_call

if TYPE_CHECKING:
    from engine.execution.hooks import HookRegistry
    from engine.llm.port import LLMPort
    from engine.safety.tool_guard import ToolGuard
    from engine.tool.registry import ToolRegistry


class IncompleteAgentRunError(RuntimeError):
    """Raised when a collected run ends incomplete, carrying the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Agent run ended incomplete: {reason}")


class FailedAgentRunError(RuntimeError):
    """Raised when a collected run ends in hard failure, carrying the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Agent run failed: {reason}")


_CURRENT_TIME_TOOL = "get_current_time"
_CURRENT_TIME_TOOL_ACK = "Time data was added to the user context."
_TOOL_SCHEMA_LOADER = "get_tool_schema"


def _tool_schema_loader_definition() -> dict:
    """Return the one stable provider tool used to load a capability contract."""
    return {
        "type": "function",
        "function": {
            "name": _TOOL_SCHEMA_LOADER,
            "description": (
                "Load the complete input schema for an available tool before using it. "
                "Pass the exact tool name from the Available Tools list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of the tool whose schema is needed.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


def _requested_tool_schema(
    tool_registry: "ToolRegistry", arguments: dict,
) -> tuple[str | None, dict | None, str | None]:
    """Resolve a visible tool schema without exposing disabled capabilities."""
    name = arguments.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, None, "`name` must be a non-empty tool name."
    requested = name.strip()
    schemas = tool_registry.get_schemas(enabled=[requested])
    if len(schemas) != 1:
        return requested, None, f"Tool unavailable: {requested}"
    schema = schemas[0]
    function = schema.get("function")
    canonical_name = function.get("name") if isinstance(function, dict) else None
    if not isinstance(canonical_name, str):
        return requested, None, f"Tool unavailable: {requested}"
    return canonical_name, schema, None


def _tool_result_messages(tool_name: str, call_id: str, content: str, *, is_error: bool) -> list[dict[str, str]]:
    """Keep tool-call protocol valid while placing live time in user context.

    Providers require a matching ``tool`` turn for every assistant tool call.
    The actual volatile value is also appended as a user turn, per product
    policy, so it never alters the stable system-prompt prefix.
    """
    if tool_name == _CURRENT_TIME_TOOL and not is_error:
        return [
            {"role": "tool", "tool_call_id": call_id, "content": _CURRENT_TIME_TOOL_ACK},
            {"role": "user", "content": f"[Current time]\n{content}"},
        ]
    return [{"role": "tool", "tool_call_id": call_id, "content": content}]


@dataclass
class _StreamedToolCall:
    id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)


@dataclass
class _ProviderResponseAccumulator:
    """Reassemble one typed provider stream into the existing ChatResponse."""

    text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls: dict[int, _StreamedToolCall] = field(default_factory=dict)
    usage: dict | None = None
    finish_reason: str | None = None
    raw_finish_reason: str | None = None
    streamed_text: bool = False
    model: str = ""

    def add(self, event: ProviderEvent) -> None:
        if event.type == ProviderEventType.OUTPUT_TEXT_DELTA:
            delta = event.data.get("delta")
            if isinstance(delta, str) and delta:
                self.text_parts.append(delta)
                self.streamed_text = True
            return

        if event.type == ProviderEventType.REASONING_DELTA:
            delta = event.data.get("delta")
            if isinstance(delta, str) and delta:
                self.reasoning_parts.append(delta)
            return

        if event.type == ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA:
            index = event.data.get("index")
            if not isinstance(index, int):
                index = 0
            call = self.tool_calls.setdefault(index, _StreamedToolCall())
            call_id = event.data.get("id")
            if isinstance(call_id, str) and call_id:
                call.id = call_id
            name = event.data.get("name")
            if isinstance(name, str) and name:
                call.name = name
            arguments_delta = event.data.get("arguments_delta")
            if isinstance(arguments_delta, str):
                call.argument_parts.append(arguments_delta)
            return

        if event.type == ProviderEventType.USAGE:
            usage = event.data.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            return

        if event.type == ProviderEventType.RESPONSE_COMPLETED:
            finish_reason = event.data.get("finish_reason")
            raw_finish_reason = event.data.get("raw_finish_reason")
            self.finish_reason = finish_reason if isinstance(finish_reason, str) else None
            self.raw_finish_reason = raw_finish_reason if isinstance(raw_finish_reason, str) else None
            model = event.data.get("model")
            if isinstance(model, str) and model:
                self.model = model

    def build(self) -> ChatResponse:
        tool_calls: list[ToolCallData] = []
        for index in sorted(self.tool_calls):
            streamed_call = self.tool_calls[index]
            if self.finish_reason == "length":
                # The caller will turn this into an explicit incomplete run and
                # will not execute the placeholder.  Keeping a marker here is
                # safer than failing while parsing a known-truncated call and
                # accidentally collapsing it into a generic transport error.
                tool_calls.append(ToolCallData(
                    id=streamed_call.id or f"partial-call-{index}",
                    name=streamed_call.name or "__incomplete_tool_call__",
                    arguments={},
                ))
                continue
            if not streamed_call.id or not streamed_call.name:
                raise RuntimeError("Provider stream ended with incomplete tool-call metadata.")
            arguments_json = "".join(streamed_call.argument_parts) or "{}"
            try:
                arguments = json.loads(arguments_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Provider stream ended with invalid tool-call arguments.") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError("Provider stream returned non-object tool-call arguments.")
            tool_calls.append(ToolCallData(
                id=streamed_call.id,
                name=streamed_call.name,
                arguments=arguments,
            ))

        return ChatResponse(
            text="".join(self.text_parts),
            reasoning="".join(self.reasoning_parts),
            tool_calls=tool_calls,
            usage=self.usage,
            finish_reason=self.finish_reason,
            raw_finish_reason=self.raw_finish_reason,
            model=self.model,
        )


def _raw_response_event(
    event: ProviderEvent,
    *,
    provision_id: str | None = None,
) -> ExecutionEvent:
    data: dict[str, object] = {"type": event.type.value, "data": event.data}
    if provision_id:
        data["provision_id"] = provision_id
    return ExecutionEvent(
        EventType.RAW_RESPONSE_EVENT,
        data,
    )


def _text_event(text: str, *, already_streamed: bool = False) -> ExecutionEvent:
    data: dict[str, object] = {"text": text}
    if already_streamed:
        data["already_streamed"] = True
    return ExecutionEvent(EventType.TEXT_DELTA, data)


def _provisional_text_event(provision_id: str, text: str) -> ExecutionEvent:
    return ExecutionEvent(EventType.PROVISIONAL_TEXT_DELTA, {
        "provision_id": provision_id,
        "text": text,
    })


def _provisional_commit_event(provision_id: str) -> ExecutionEvent:
    return ExecutionEvent(EventType.PROVISIONAL_COMMIT, {"provision_id": provision_id})


def _provisional_retract_event(provision_id: str, reason: str) -> ExecutionEvent:
    return ExecutionEvent(EventType.PROVISIONAL_RETRACT, {
        "provision_id": provision_id,
        "reason": reason,
    })


def _usage_event_data(usage: dict | None) -> dict | None:
    normalized = normalize_usage(usage)
    if not any(normalized.values()):
        return None
    # Detail keys (cache/reasoning) appear only when non-zero so traces stay
    # compact and pre-existing three-key consumers see an unchanged payload.
    return {
        key: value
        for key, value in normalized.items()
        if value or key in ("input_tokens", "output_tokens", "total_tokens")
    }


def _context_usage_event(
    receipt: ContextReceipt,
    *,
    input_tokens: int | None = None,
    fit_status: str,
    actions: tuple[str, ...] = (),
) -> ExecutionEvent:
    """Report selected-route capacity, request cost, and what fitting dropped.

    ``fit_status`` alone cannot distinguish a cheap tool-output prune from an
    LLM re-summary of the whole history, and ``recovered`` means messages were
    deleted outright.  ``actions`` used to reach the trace only when the
    request ended up UNFIT — the successful-but-lossy path recorded that the
    conversation shrank without recording what left it.
    """
    estimated = not isinstance(input_tokens, int) or input_tokens <= 0
    context_tokens = (
        input_tokens if not estimated else receipt.estimated_input_tokens
    )
    context_percent = round(
        min(context_tokens, receipt.safe_input_budget)
        / receipt.safe_input_budget
        * 100
    )
    return ExecutionEvent(EventType.CONTEXT_USAGE, {
        "context_tokens": context_tokens,
        "context_window": receipt.model_context_window,
        "context_percent": context_percent,
        "estimated": estimated,
        "message_tokens": receipt.message_tokens,
        "tool_schema_tokens": receipt.tool_schema_tokens,
        "protocol_tokens": receipt.protocol_tokens,
        "effective_context_window": receipt.effective_context_window,
        "safe_input_budget": receipt.safe_input_budget,
        "output_reserve": receipt.output_reserve,
        "safety_margin": receipt.safety_margin,
        "window_declared": receipt.window_declared,
        "output_limit_declared": receipt.output_limit_declared,
        "fit_status": fit_status,
        "actions": list(actions),
    })


def _is_context_limit_error(error: BaseException) -> bool:
    if isinstance(error, LLMContextLengthError):
        return True
    if not isinstance(error, LLMResponseError):
        return False
    message = str(error).casefold()
    return any(marker in message for marker in (
        "context_length_exceeded",
        "context length",
        "context limit",
        "maximum context",
        "max context",
        "input length",
        "prompt is too long",
        "input is too long",
        "too many tokens",
    ))


def _recover_context_after_provider_rejection(
    conversation: list[dict],
    llm: object,
) -> list[dict]:
    input_budget, _ = compaction_policy_for_llm(llm)
    # Keep a second cushion for request framing the local estimator cannot see
    # (tool schemas, provider envelopes, and tokenizer differences).
    return trim_conversation_for_context_limit(
        conversation,
        token_budget=max(1, int(input_budget * 0.65)),
    )


def _should_repair_incomplete_final(
    text: str,
    *,
    had_successful_tool: bool,
    repair_count: int,
) -> bool:
    return (
        had_successful_tool
        and repair_count < MAX_INCOMPLETE_FINAL_REPAIRS
        and looks_like_incomplete_final_after_tool(text)
    )


def _assistant_turn(text: str, reasoning: str = "") -> dict[str, object]:
    """构造一条 assistant 消息，按需带上 reasoning。

    推理模型（thinking 模式）要求本轮 reasoning_content 原样回传，否则**下一次**请求会被
    provider 整个以 400 拒收（实测 deepseek-v4-pro："The `reasoning_content` in the
    thinking mode must be passed back to the API"）。非推理模型 reasoning 为空 → 字段
    不出现，同一条路径同时支持两类模型，无需探测模型能力。

    每一条"追加 assistant 消息后还会再发请求"的路径都必须走这里。收敛在一处是因为
    第一次修复只补了工具调用那条，漏掉了长度续写与残句修复 —— 加第四条路径时别再漏。
    conversation 本身是 OpenAI wire format（K6 记录的既有边界），故用 provider 字段名；
    anthropic adapter 的翻译循环按 role 挑字段，会忽略它。
    """
    message: dict[str, object] = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _append_incomplete_final_repair(
    conversation: list[dict], text: str, reasoning: str = ""
) -> None:
    conversation.append(_assistant_turn(text, reasoning))
    conversation.append({"role": "system", "content": INCOMPLETE_FINAL_AFTER_TOOL_HINT})


def _append_length_continuation(
    conversation: list[dict], text: str, reasoning: str = ""
) -> None:
    conversation.append(_assistant_turn(text, reasoning))
    conversation.append({"role": "system", "content": CONTINUE_AFTER_LENGTH_HINT})


# The two text-collecting adapters that used to sit here (``react_loop`` and
# ``react_stream_loop``) had no production caller: orchestration consumes
# ``react_event_loop`` events directly.  They now live beside the tests that
# wanted a plain string — engine/tests/execution/react_text_adapters.py.


async def react_event_loop(
    llm: "LLMPort",
    messages: list[dict],
    tool_registry: "ToolRegistry",
    tool_guard: "ToolGuard | None" = None,
    max_iters: int = DEFAULT_MAX_REACT_ITERS,
    *,
    fact_gate: FactGate | None = None,
    provisional_lifecycle: bool = True,
    prefix_cache_key: str | None = None,
    hook_registry: "HookRegistry | None" = None,
) -> AsyncGenerator[ExecutionEvent, None]:
    """ReAct loop: model response, tool calls, results, and next turn in one history."""
    lazy_tool_schemas = bool(getattr(tool_registry, "lazy_tool_schemas", False))
    if lazy_tool_schemas:
        # Tool descriptions live in the stable prompt prefix. The provider sees
        # only this tiny loader schema until the model explicitly asks for one
        # capability, keeping the large JSON contracts out of every initial turn.
        tools: list[dict] | None = (
            [_tool_schema_loader_definition()] if tool_registry.list_tools() else None
        )
    else:
        tools = tool_registry.get_schemas() or None
    visible_tool_names = {
        schema["function"]["name"]
        for schema in (tools or [])
        if isinstance(schema.get("function"), dict)
        and isinstance(schema["function"].get("name"), str)
    }
    # 逐条浅拷贝：prune/compress 会原地改写 content，
    # 不复制会污染调用方传入的 history dict（跨请求复用时留脏数据）。
    conversation = [dict(m) for m in messages]
    policy = ToolPolicy(
        tool_guard,
        fact_gate=fact_gate if fact_gate is not None else current_fact_gate(),
    )
    consecutive_errors = 0
    productive_iters = 0
    recovery_iters = 0
    preflight_iters = 0
    had_successful_tool = False
    incomplete_final_repairs = 0
    length_continuations = 0
    final_text_parts: list[str] = []
    final_text_was_streamed = False
    active_provision_ids: list[str] = []
    last_error_key: str | None = None
    identical_error_count = 0
    context_recoveries = 0
    model_compaction_enabled = True

    while productive_iters < max_iters:
        if len(conversation) > CONVERSATION_HARD_LIMIT:
            # ponytail: keep head (system + initial user) and recent tail
            head = conversation[:CONVERSATION_KEEP_HEAD]
            # 切点落在 tool 结果串中会拆散 assistant(tool_calls)/tool 配对
            # （provider 400）。向前回退到 assistant/user 边界：同一轮的 tool
            # 结果之间可能夹着 system 提示（TOOL_FAILURE_HINT 在 tool_calls
            # 循环内 append），只认 role=="tool" 会在提示处停下留下孤儿。
            cut = len(conversation) - CONVERSATION_KEEP_RECENT
            while cut > CONVERSATION_KEEP_HEAD and conversation[cut].get("role") in ("tool", "system"):
                cut -= 1
            tail = conversation[cut:]
            while head and head[-1].get("role") == "assistant" and head[-1].get("tool_calls"):
                head.pop()
            conversation = head + tail

        pre_fit_receipt = measure_request(conversation, tools, llm)
        compression_started = (
            pre_fit_receipt.message_tokens
            >= pre_fit_receipt.compaction_trigger
        )
        if compression_started:
            yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_START)
        fit = await fit_request(
            conversation,
            tools,
            llm,
            prefix_cache_key=prefix_cache_key,
            allow_model_compaction=model_compaction_enabled,
        )
        if any(
            action in {"compaction_failed", "compaction_rejected"}
            for action in fit.actions
        ):
            model_compaction_enabled = False
        fit_changed = list(fit.messages) != conversation
        if fit_changed and not compression_started:
            yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_START)
        conversation = [dict(message) for message in fit.messages]
        tools = [dict(tool) for tool in fit.tools] if fit.tools else None
        if compression_started or fit_changed:
            yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_END)
        yield _context_usage_event(
            fit.receipt,
            fit_status=fit.status.value,
            actions=fit.actions,
        )
        if not fit.fits:
            for provision_id in active_provision_ids:
                yield _provisional_retract_event(
                    provision_id,
                    "context_capacity_exhausted",
                )
            active_provision_ids.clear()
            yield ExecutionEvent(EventType.INCOMPLETE, {
                "reason": "context_capacity_exhausted",
                "fit_status": fit.status.value,
                "estimated_input_tokens": fit.receipt.estimated_input_tokens,
                "safe_input_budget": fit.receipt.safe_input_budget,
                "model_context_window": fit.receipt.model_context_window,
                "output_reserve": fit.receipt.output_reserve,
                "actions": list(fit.actions),
            })
            return

        yield ExecutionEvent(EventType.THINKING, {})
        provider_prefix_cache_key = (
            fit.prefix_cache_key
            if bool(
                getattr(
                    getattr(llm, "capabilities", None),
                    "prefix_cache_key",
                    False,
                )
            )
            else None
        )
        response_text_was_streamed = False
        response_provision_id: str | None = None
        stream_events = getattr(llm, "chat_events", None)
        if getattr(llm, "stream", False) and callable(stream_events):
            accumulator = _ProviderResponseAccumulator()
            saw_content_event = False
            if provisional_lifecycle:
                response_provision_id = uuid4().hex
            try:
                provider_stream = (
                    stream_events(
                        conversation,
                        tools=tools,
                        prefix_cache_key=provider_prefix_cache_key,
                    )
                    if provider_prefix_cache_key
                    else stream_events(conversation, tools=tools)
                )
                async for provider_event in provider_stream:
                    accumulator.add(provider_event)
                    is_text_delta = provider_event.type == ProviderEventType.OUTPUT_TEXT_DELTA
                    raw_provision_id = response_provision_id if is_text_delta else None
                    yield _raw_response_event(provider_event, provision_id=raw_provision_id)
                    if raw_provision_id:
                        delta = provider_event.data.get("delta")
                        if isinstance(delta, str) and delta:
                            if raw_provision_id not in active_provision_ids:
                                active_provision_ids.append(raw_provision_id)
                            yield _provisional_text_event(raw_provision_id, delta)
                    # A streamed reasoning delta is not user-visible, but it
                    # still proves the provider has begun a response.  A
                    # fallback would replay that turn and can produce a
                    # different tool plan, so only retry a stream that failed
                    # before every semantic response delta.
                    if not saw_content_event and provider_event.type in (
                        ProviderEventType.OUTPUT_TEXT_DELTA,
                        ProviderEventType.REASONING_DELTA,
                        ProviderEventType.FUNCTION_CALL_ARGUMENTS_DELTA,
                    ):
                        saw_content_event = True
            except Exception as stream_exc:
                if _is_context_limit_error(stream_exc):
                    if context_recoveries >= 1:
                        for provision_id in active_provision_ids:
                            yield _provisional_retract_event(
                                provision_id,
                                "context_limit",
                            )
                        active_provision_ids.clear()
                        yield ExecutionEvent(EventType.INCOMPLETE, {
                            "reason": "context_limit",
                            "recoveries": context_recoveries,
                        })
                        return
                    context_recoveries += 1
                    # The abandoned draft was streamed to the client but is
                    # being discarded by this recovery.  Retract it before the
                    # retried stream runs, or both the old and new ids would be
                    # committed at finish and the client would keep rendering
                    # text that no longer exists.
                    for provision_id in active_provision_ids:
                        yield _provisional_retract_event(
                            provision_id,
                            "context_limit",
                        )
                    active_provision_ids.clear()
                    yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_START)
                    conversation = _recover_context_after_provider_rejection(
                        conversation,
                        llm,
                    )
                    yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_END)
                    continue
                # Fallback to non-streaming only if this response emitted no
                # semantic delta and no earlier continuation is still visible
                # as a provisional draft.  Otherwise the fallback could
                # replay an unseen suffix or a different tool plan.
                if saw_content_event or active_provision_ids:
                    for provision_id in active_provision_ids:
                        yield _provisional_retract_event(provision_id, "stream_error")
                    active_provision_ids.clear()
                    raise
                try:
                    response = await (
                        llm.chat(
                            conversation,
                            tools=tools,
                            prefix_cache_key=provider_prefix_cache_key,
                        )
                        if provider_prefix_cache_key
                        else llm.chat(conversation, tools=tools)
                    )
                except Exception as exc:
                    if not _is_context_limit_error(exc):
                        raise
                    if context_recoveries >= 1:
                        yield ExecutionEvent(EventType.INCOMPLETE, {
                            "reason": "context_limit",
                            "recoveries": context_recoveries,
                        })
                        return
                    context_recoveries += 1
                    yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_START)
                    conversation = _recover_context_after_provider_rejection(conversation, llm)
                    yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_END)
                    continue
            else:
                try:
                    response = accumulator.build()
                except Exception:
                    for provision_id in active_provision_ids:
                        yield _provisional_retract_event(provision_id, "stream_error")
                    active_provision_ids.clear()
                    raise
                response_text_was_streamed = accumulator.streamed_text
        else:
            try:
                response = await (
                    llm.chat(
                        conversation,
                        tools=tools,
                        prefix_cache_key=provider_prefix_cache_key,
                    )
                    if provider_prefix_cache_key
                    else llm.chat(conversation, tools=tools)
                )
            except Exception as exc:
                if not _is_context_limit_error(exc):
                    raise
                if context_recoveries >= 1:
                    yield ExecutionEvent(EventType.INCOMPLETE, {
                        "reason": "context_limit",
                        "recoveries": context_recoveries,
                    })
                    return
                context_recoveries += 1
                yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_START)
                conversation = _recover_context_after_provider_rejection(conversation, llm)
                yield ExecutionEvent(EventType.CONTEXT_COMPRESSION_END)
                continue
        usage = _usage_event_data(response.usage)
        if usage:
            yield ExecutionEvent(EventType.TOKEN_USAGE, usage)
        yield _context_usage_event(
            fit.receipt,
            input_tokens=usage.get("input_tokens") if usage else None,
            fit_status=fit.status.value,
            actions=fit.actions,
        )

        thought = (response.reasoning or (response.text if response.has_tool_calls else "")).strip()
        if thought:
            yield ExecutionEvent(EventType.THINKING, {"text": thought, "done": True})

        if response.has_tool_calls:
            # Any pending draft is a potential final response.  A tool call
            # re-enters evidence gathering, so neither a current preamble nor
            # an earlier length-continuation fragment may become durable text.
            for provision_id in active_provision_ids:
                yield _provisional_retract_event(provision_id, "tool_call_pending")
            active_provision_ids.clear()
            final_text_parts.clear()
            final_text_was_streamed = False

        if response.finish_reason == "length" and response.has_tool_calls:
            # A length-limited tool call may have incomplete JSON arguments.
            # Never execute it or append a generic continuation prompt that
            # could change the requested action.
            # 上面的 has_tool_calls 分支已把草稿 retract（tool_call_pending），
            # 屏幕上的流式文本已被消费方删除；这里若再打 already_streamed
            # 标记，消费方会跳过渲染 → 文本落库但用户永远看不到。
            if response.text:
                yield _text_event(response.text)
            yield ExecutionEvent(EventType.INCOMPLETE, {
                "reason": "model_output_limit",
                "phase": "tool_call",
                "continuations": length_continuations,
            })
            return

        if response.has_tool_calls and response.finish_reason in {"content_filter", "other", "error"}:
            # 同上：草稿已 retract，不能再标 already_streamed。
            if response.text:
                yield _text_event(response.text)
            if response.finish_reason == "error":
                yield ExecutionEvent(EventType.FAILED, {"reason": "provider_finish_error"})
            else:
                yield ExecutionEvent(EventType.INCOMPLETE, {
                    "reason": (
                        "content_filter"
                        if response.finish_reason == "content_filter"
                        else "unknown_provider_finish_reason"
                    ),
                    "raw_finish_reason": response.raw_finish_reason,
                })
            return

        if not response.has_tool_calls:
            if response.text:
                final_text_parts.append(response.text)
                final_text_was_streamed = final_text_was_streamed or response_text_was_streamed
            final_text = "".join(final_text_parts)

            if response.finish_reason == "length":
                if length_continuations < MAX_LENGTH_CONTINUATIONS:
                    length_continuations += 1
                    _append_length_continuation(
                        conversation, response.text, response.reasoning
                    )
                    continue
                if final_text:
                    for provision_id in active_provision_ids:
                        yield _provisional_commit_event(provision_id)
                    active_provision_ids.clear()
                    yield _text_event(final_text, already_streamed=final_text_was_streamed)
                yield ExecutionEvent(EventType.INCOMPLETE, {
                    "reason": "model_output_limit",
                    "continuations": length_continuations,
                })
                return

            if response.finish_reason == "content_filter":
                if active_provision_ids:
                    for provision_id in active_provision_ids:
                        yield _provisional_retract_event(provision_id, "content_filter")
                    active_provision_ids.clear()
                elif final_text:
                    yield _text_event(final_text, already_streamed=final_text_was_streamed)
                yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "content_filter"})
                return

            if response.finish_reason == "error":
                for provision_id in active_provision_ids:
                    yield _provisional_retract_event(provision_id, "provider_finish_error")
                active_provision_ids.clear()
                yield ExecutionEvent(EventType.FAILED, {"reason": "provider_finish_error"})
                return

            if response.finish_reason == "other":
                if active_provision_ids:
                    for provision_id in active_provision_ids:
                        yield _provisional_retract_event(provision_id, "unknown_provider_finish_reason")
                    active_provision_ids.clear()
                elif final_text:
                    yield _text_event(final_text, already_streamed=final_text_was_streamed)
                yield ExecutionEvent(EventType.INCOMPLETE, {
                    "reason": "unknown_provider_finish_reason",
                    "raw_finish_reason": response.raw_finish_reason,
                })
                return

            if _should_repair_incomplete_final(
                final_text,
                had_successful_tool=had_successful_tool,
                repair_count=incomplete_final_repairs,
            ):
                for provision_id in active_provision_ids:
                    yield _provisional_retract_event(provision_id, "incomplete_final_repair")
                active_provision_ids.clear()
                incomplete_final_repairs += 1
                _append_incomplete_final_repair(
                    conversation, final_text, response.reasoning
                )
                final_text_parts.clear()
                final_text_was_streamed = False
                continue
            if final_text:
                for provision_id in active_provision_ids:
                    yield _provisional_commit_event(provision_id)
                active_provision_ids.clear()
                yield _text_event(final_text, already_streamed=final_text_was_streamed)
            else:
                # A successful tool call is evidence gathering, not a valid
                # chat completion.  The caller still needs a final assistant
                # response to render and persist.
                yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "empty_model_response"})
            return

        policy.begin_round()
        assistant_message = _assistant_turn(response.text, response.reasoning)
        assistant_message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
            }
            for tc in response.tool_calls
        ]
        conversation.append(assistant_message)

        round_had_success = False
        round_had_failure = False
        round_had_preflight = False
        for tc in response.tool_calls:
            if lazy_tool_schemas and tc.name == _TOOL_SCHEMA_LOADER:
                requested_name, schema, error = _requested_tool_schema(
                    tool_registry, tc.arguments,
                )
                content = (
                    json.dumps(schema, ensure_ascii=False, sort_keys=True)
                    if schema is not None
                    else f"Error: {error}"
                )
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                })
                yield ExecutionEvent(EventType.TOOL_CALL_START, {
                    "name": tc.name,
                    "id": tc.id,
                    "arguments": tc.arguments,
                })
                yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                    "id": tc.id,
                    "name": tc.name,
                    "error": schema is None,
                    "blocked": False,
                    "preflight": False,
                    "content": content[:200],
                    "error_kind": "tool_unavailable" if schema is None else None,
                    "retryable": False,
                    "timed_out": False,
                    "side_effect_status": "none",
                    "metadata": {},
                })
                if schema is None:
                    round_had_failure = True
                    consecutive_errors += 1
                else:
                    assert requested_name is not None
                    visible_tool_names.add(requested_name)
                    if tools is None:
                        tools = [_tool_schema_loader_definition(), schema]
                    elif not any(
                        isinstance(candidate.get("function"), dict)
                        and candidate["function"].get("name") == requested_name
                        for candidate in tools
                    ):
                        # Provider adapters may retain the argument list for
                        # diagnostics; replace rather than mutate it so a past
                        # request remains an accurate snapshot of its tools.
                        tools = [*tools, schema]
                    # Loading a schema consumes an iteration, but is not an
                    # evidence-gathering tool success: a following text answer
                    # is still valid even when no operational tool ran.
                    round_had_success = True
                    consecutive_errors = 0
                continue

            call = tool_registry.normalize_call(
                ToolCall(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                )
            )

            # Lazy schemas exist to keep large JSON contracts out of the stable
            # prompt prefix; they are not a permission boundary.  A model that
            # calls a tool by name without first loading its schema is asking
            # for a capability the profile/identity allowlist already grants, so
            # complete the handshake here.  Reporting it as ``Tool disabled``
            # read as terminal and made runs abandon a working tool for the rest
            # of the conversation.  ``_requested_tool_schema`` resolves through
            # ``get_schemas``, so a pipeline-scoped registry still refuses a
            # capability its node never had.
            if lazy_tool_schemas and call.name not in visible_tool_names:
                loaded_name, loaded_schema, _ = _requested_tool_schema(
                    tool_registry, {"name": call.name},
                )
                if loaded_schema is not None and loaded_name is not None:
                    visible_tool_names.add(loaded_name)
                    tools = [*(tools or []), loaded_schema]

            # ``render_ui`` is an engine-owned presentation capability. It is
            # deliberately not executed like a provider tool: rendering it
            # cannot write files, call the network, or make arbitrary JSON a
            # client-side component tree. The validated event is sent only to
            # clients that explicitly understand the smith-ui contract.
            if call.name == "render_ui":
                if call.name not in visible_tool_names:
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": "Error: render_ui is disabled for this agent",
                    })
                    yield ExecutionEvent(EventType.TOOL_CALL_START, {
                        "name": call.name,
                        "id": tc.id,
                        "arguments": call.arguments,
                    })
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                        "id": tc.id,
                        "error": True,
                        "blocked": False,
                        "preflight": False,
                        "content": "render_ui is disabled for this agent",
                    })
                    round_had_failure = True
                    consecutive_errors += 1
                    continue

                decision = policy.evaluate(call)
                if not decision.allowed:
                    conversation.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                    yield ExecutionEvent(EventType.TOOL_CALL_START, {
                        "name": call.name,
                        "id": tc.id,
                        "arguments": call.arguments,
                    })
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                        "id": tc.id,
                        "error": False,
                        "blocked": True,
                        "preflight": False,
                        "reason": decision.reason,
                    })
                    round_had_failure = True
                    consecutive_errors += 1
                    continue

                validated = validate_smith_ui_call(
                    call.arguments,
                    working_dir=tool_registry.working_directory,
                )
                if not validated.ok or validated.payload is None:
                    reason = validated.reason or "Invalid smith-ui payload"
                    conversation.append({"role": "tool", "tool_call_id": call.id, "content": f"Error: {reason}"})
                    yield ExecutionEvent(EventType.SMITH_UI_FALLBACK, smith_ui_fallback(call.arguments, reason))
                    round_had_failure = True
                    consecutive_errors += 1
                    continue

                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "Smith UI rendered successfully. Do not repeat its contents as raw JSON.",
                })
                yield ExecutionEvent(EventType.SMITH_UI, validated.payload)
                round_had_success = True
                had_successful_tool = True
                consecutive_errors = 0
                last_error_key = None
                identical_error_count = 0
                continue

            # A node-scoped registry deliberately hides capabilities that are
            # not part of this stage.  Reject a hallucinated or disabled call
            # before ToolPolicy sees it: a capability that cannot execute must
            # never turn into an approval request merely because it exists in
            # the broader identity-level registry.
            if call.name not in visible_tool_names:
                unavailable_message = getattr(
                    tool_registry,
                    "unavailable_tool_message",
                    None,
                )
                content = (
                    unavailable_message(call.name)
                    if callable(unavailable_message)
                    else f"Tool disabled: {call.name}"
                )
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })
                yield ExecutionEvent(EventType.TOOL_CALL_START, {
                    "name": tc.name,
                    "id": tc.id,
                    "arguments": call.arguments,
                })
                yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                    "id": tc.id,
                    "error": True,
                    "blocked": False,
                    "preflight": False,
                    "content": content[:200],
                    "error_kind": "tool_disabled",
                    "retryable": False,
                    "timed_out": False,
                    "side_effect_status": "none",
                    "metadata": {},
                })
                round_had_failure = True
                consecutive_errors += 1
                error_key = f"{tc.name}:{content[:120]}"
                if error_key == last_error_key:
                    identical_error_count += 1
                else:
                    last_error_key = error_key
                    identical_error_count = 1
                if identical_error_count >= MAX_IDENTICAL_TOOL_ERRORS:
                    yield ExecutionEvent(
                        EventType.TEXT_DELTA,
                        {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                    )
                    yield ExecutionEvent(EventType.INCOMPLETE, {
                        "reason": "identical_tool_error_loop",
                    })
                    return
                if consecutive_errors >= 3:
                    conversation.append({"role": "system", "content": TOOL_FAILURE_HINT})
                    consecutive_errors = 0
                continue

            granted_approval_id: str | None = None
            yield ExecutionEvent(EventType.TOOL_CALL_START, {"name": tc.name, "id": tc.id, "arguments": call.arguments})

            decision = policy.evaluate(call)
            if not decision.allowed:
                if decision.challenged:
                    conversation.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                        "id": tc.id,
                        "error": False,
                        "blocked": False,
                        "preflight": True,
                        "reason": decision.reason,
                        "level": decision.level.value,
                    })
                    round_had_preflight = True
                    continue
                if decision.approval_required:
                    approval_context = current_approval_context()
                    if approval_context is not None:
                        broker, run_id = approval_context
                        arguments_summary = summarize_arguments(call.arguments)
                        definition = tool_registry.definitions().get(call.name)
                        presentation = build_approval_presentation(
                            call.name,
                            decision.level.value,
                            decision.reason,
                            arguments_summary,
                            tool_description=definition.description if definition else "",
                            scope=decision.approval_scope,
                        )
                        approval_request = broker.open(
                            ApprovalRequest(
                                approval_id=uuid4().hex,
                                run_id=run_id,
                                tool_name=call.name,
                                level=decision.level.value,
                                reason=decision.reason,
                                arguments_summary=arguments_summary,
                                scope=decision.approval_scope,
                                risk=decision.risk,
                                presentation=presentation,
                            )
                        )
                        yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                            "id": tc.id,
                            "error": False,
                            "blocked": True,
                            "preflight": False,
                            "reason": approval_request.reason,
                            "level": approval_request.level,
                            "needs_confirmation": True,
                            "approval_required": True,
                            "approval_id": approval_request.approval_id,
                            "tool": approval_request.tool_name,
                            "arguments": approval_request.arguments_summary,
                            "risk": approval_request.risk.value if approval_request.risk is not None else None,
                            "presentation": presentation.to_dict(),
                            "scope": (
                                approval_request.scope.to_dict()
                                if approval_request.scope is not None
                                else None
                            ),
                        })
                        try:
                            approved = await broker.wait(approval_request)
                            denial = "User denied approval"
                            approval_outcome = "denied"
                        except ApprovalTimeoutError:
                            approved = False
                            denial = "Approval timed out"
                            approval_outcome = "timed_out"
                        if approved:
                            # The hard guard already passed. Continue with the
                            # exact suspended call instead of asking the model
                            # to recreate it and risking a duplicate side effect.
                            granted_approval_id = approval_request.approval_id
                        else:
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": f"[BLOCKED] {denial}",
                            })
                            conversation.append({"role": "system", "content": tool_blocked_prompt()})
                            yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                                "id": tc.id,
                                "error": False,
                                "blocked": True,
                                "preflight": False,
                                "reason": denial,
                                "level": decision.level.value,
                                "needs_confirmation": False,
                                "approval_id": approval_request.approval_id,
                                "approval_outcome": approval_outcome,
                                "risk": decision.risk.value,
                            })
                            round_had_failure = True
                            consecutive_errors += 1
                            continue
                    else:
                        conversation.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                        conversation.append({"role": "system", "content": tool_blocked_prompt()})
                        yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                            "id": tc.id,
                            "error": False,
                            "blocked": True,
                            "preflight": False,
                            "reason": "Approval broker unavailable",
                            "level": decision.level.value,
                            "needs_confirmation": True,
                            "risk": decision.risk.value,
                        })
                        round_had_failure = True
                        consecutive_errors += 1
                        continue
                else:
                    conversation.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                    conversation.append({"role": "system", "content": tool_blocked_prompt()})
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                        "id": tc.id,
                        "error": False,
                        "blocked": True,
                        "preflight": False,
                        "reason": decision.reason,
                        "level": decision.level.value,
                        "needs_confirmation": decision.needs_confirmation,
                        "risk": decision.risk.value,
                    })
                    round_had_failure = True
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        conversation.append({"role": "system", "content": TOOL_FAILURE_HINT})
                        consecutive_errors = 0
                    continue

            # ========== PreToolUse Hook 检查 ==========
            if hook_registry is not None:
                allowed, denial_reason = await hook_registry.run_pre_hooks(
                    call.name,
                    call.arguments
                )
                if not allowed:
                    # Pre Hook 阻止了工具执行。
                    # 每个 assistant.tool_calls 条目都必须有配对的 tool 结果消息，
                    # 否则**下一次**请求整个被 provider 拒收（OpenAI 400；Anthropic
                    # "tool_use ids were found without tool_result blocks"）。这条
                    # 分支曾是本循环里唯一漏掉配对的阻断路径，而 config-protection
                    # 是默认开启的 PreToolHook —— 编辑 pyproject.toml / tsconfig.json
                    # 就会走到这里。
                    denial = denial_reason or "Blocked by pre-tool hook"
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"[BLOCKED] {denial}",
                    })
                    conversation.append({"role": "system", "content": tool_blocked_prompt()})
                    blocked_event: dict[str, object] = {
                        "id": tc.id,
                        "error": True,
                        "blocked": True,
                        "preflight": False,
                        "reason": denial,
                    }
                    if granted_approval_id is not None:
                        # The call already passed the approval gate; carry the
                        # resolution so run state leaves WAITING_APPROVAL
                        # instead of staying stuck with a stale approval_id.
                        blocked_event["approval_id"] = granted_approval_id
                        blocked_event["approval_outcome"] = "granted"
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, blocked_event)
                    round_had_failure = True
                    consecutive_errors += 1
                    # A hook that denies the same call every round is a loop the
                    # other error paths already bound; without this the model can
                    # retry a permanently-blocked edit until the whole iteration
                    # budget is gone.
                    error_key = f"{tc.name}:{denial[:120]}"
                    if error_key == last_error_key:
                        identical_error_count += 1
                    else:
                        last_error_key = error_key
                        identical_error_count = 1
                    if identical_error_count >= MAX_IDENTICAL_TOOL_ERRORS:
                        yield ExecutionEvent(
                            EventType.TEXT_DELTA,
                            {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                        )
                        yield ExecutionEvent(EventType.INCOMPLETE, {
                            "reason": "identical_tool_error_loop",
                        })
                        return
                    if consecutive_errors >= 3:
                        conversation.append({"role": "system", "content": TOOL_FAILURE_HINT})
                        consecutive_errors = 0
                    continue

            with tool_registry.authorize_execution(
                call,
                approval_id=granted_approval_id,
                approval_scope=decision.approval_scope,
            ):
                result = await tool_registry.execute(call)

            # ========== PostToolUse Hook 检查 ==========
            if hook_registry is not None:
                warnings = await hook_registry.run_post_hooks(
                    call.name,
                    call.arguments,
                    result
                )
                if warnings:
                    # 将警告注入到对话中（作为 system 消息）
                    warning_text = "\n\n".join(warnings)
                    conversation.append({"role": "system", "content": warning_text})

            conversation.extend(_tool_result_messages(
                call.name, result.call_id, result.content, is_error=result.is_error,
            ))
            result_event = {
                "id": tc.id,
                "name": call.name,
                "error": result.is_error,
                "blocked": False,
                "preflight": False,
                # Evidence binding: hash the FULL result (content is truncated
                # below for transport) so a gate verdict can be checked against
                # the exact output this call produced.  Only the hash is stored.
                "result_hash": tool_result_hash(
                    tool_name=call.name,
                    call_id=result.call_id,
                    arguments=call.arguments,
                    content=result.content,
                    is_error=result.is_error,
                ),
                "content": result.content[:200],
                "error_kind": result.error_kind,
                "retryable": result.retryable,
                "timed_out": result.timed_out,
                "side_effect_status": result.side_effect_status,
                "metadata": result.metadata,
            }
            if granted_approval_id is not None:
                result_event["approval_outcome"] = "granted"
                result_event["approval_id"] = granted_approval_id
            yield ExecutionEvent(EventType.TOOL_CALL_RESULT, result_event)
            if result.is_error:
                round_had_failure = True
                consecutive_errors += 1
                error_key = f"{tc.name}:{result.content[:120]}"
                if error_key == last_error_key:
                    identical_error_count += 1
                else:
                    last_error_key = error_key
                    identical_error_count = 1
                if identical_error_count >= MAX_IDENTICAL_TOOL_ERRORS:
                    yield ExecutionEvent(
                        EventType.TEXT_DELTA,
                        {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                    )
                    yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "identical_tool_error_loop"})
                    return
                if consecutive_errors >= 3:
                    conversation.append({"role": "system", "content": TOOL_FAILURE_HINT})
                    consecutive_errors = 0
            else:
                round_had_success = True
                had_successful_tool = True
                consecutive_errors = 0
                last_error_key = None
                identical_error_count = 0

        if round_had_preflight:
            preflight_iters += 1
            if preflight_iters >= MAX_PREFLIGHT_CHALLENGE_ITERS:
                yield ExecutionEvent(
                    EventType.TEXT_DELTA,
                    {"text": budget_exhausted_message(PREFLIGHT_BUDGET_MESSAGE)},
                )
                yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "preflight_budget"})
                return

        if round_had_success:
            productive_iters += 1
            continue

        if round_had_failure:
            recovery_iters += 1
            if recovery_iters >= MAX_FAILED_TOOL_RECOVERY_ITERS:
                yield ExecutionEvent(
                    EventType.TEXT_DELTA,
                    {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                )
                yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "tool_failure_budget"})
                return
        # 纯 preflight 轮不计入任何预算直接进入下一轮（preflight_iters 已在上方封顶）。

    for provision_id in active_provision_ids:
        yield _provisional_retract_event(provision_id, "tool_call_budget")
    yield ExecutionEvent(
        EventType.TEXT_DELTA,
        {"text": budget_exhausted_message(TOOL_CALL_BUDGET_MESSAGE)},
    )
    yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "tool_call_budget"})
