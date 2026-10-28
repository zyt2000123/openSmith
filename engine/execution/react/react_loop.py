"""Canonical ReAct loop shared by text, stream, and skill execution paths."""

from __future__ import annotations

import json
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncGenerator
from uuid import uuid4

from engine.context import (
    ContextReceipt,
    estimate_tokens,
    fit_request,
    measure_request,
)
from engine.context.compression import (
    compaction_policy_for_llm,
    trim_conversation_for_context_limit,
)
from engine.execution.events import EventType, ExecutionEvent
from engine.llm.contracts import (
    ChatResponse,
    LLMContextLengthError,
    LLMResponseError,
    ToolCallData,
)
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.llm.usage import USAGE_REPORTED_KEY, normalize_usage
from engine.safety.fact_gate import FactGate, current_fact_gate
from engine.safety.tool_policy import ToolPolicy

from .budget import (
    CONTINUE_AFTER_LENGTH_HINT,
    DEFAULT_MAX_REACT_ITERS,
    INCOMPLETE_FINAL_AFTER_TOOL_HINT,
    MAX_COMPACTION_FAILURES,
    MAX_FAILED_TOOL_RECOVERY_ITERS,
    MAX_INCOMPLETE_FINAL_REPAIRS,
    MAX_LENGTH_CONTINUATIONS,
    MAX_PREFLIGHT_CHALLENGE_ITERS,
    PREFLIGHT_BUDGET_MESSAGE,
    TOOL_CALL_BUDGET_MESSAGE,
    TOOL_FAILURE_BUDGET_MESSAGE,
    budget_exhausted_message,
    looks_like_incomplete_final_after_tool,
    trim_conversation_to_message_cap,
)
from .tool_round import ToolRoundRunner, ToolRoundResult

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


def _final_text_delivery(parts: list[tuple[str, bool]]) -> tuple[str, bool]:
    """Join the collected fragments and say whether the client already has them.

    ``already_streamed`` means "skip rendering" to the consumer, so it may only
    be set when *every* fragment reached the client as a stream.  A stream that
    dies before its first semantic delta falls back to a non-streaming
    ``llm.chat`` call, and that fragment was never sent; if it then finishes on
    the length limit, the streamed continuation used to mark the whole join as
    already rendered — the first half was persisted but never shown.

    Reporting a mixed reply as not streamed re-renders it.  Under provisional
    lifecycle the caller retracts the streamed draft first, so nothing is
    duplicated; without it (pipeline nodes, sub-agents) the streamed fragment
    is not retractable and shows twice — a visible repeat is recoverable for
    the reader, silently losing half the answer is not.
    """
    text = "".join(part for part, _ in parts)
    return text, bool(parts) and all(streamed for _, streamed in parts)


def _final_text_events(
    final_text: str,
    *,
    streamed: bool,
    provision_ids: list[str],
) -> list[ExecutionEvent]:
    """Close the open drafts and emit the final text as one consistent pair.

    Committing a draft and then re-sending the same text unmarked renders it
    twice, so the draft is retracted exactly when the join is re-sent.
    """
    events: list[ExecutionEvent] = [
        _provisional_commit_event(provision_id)
        if streamed
        else _provisional_retract_event(provision_id, "unstreamed_fragment_pending")
        for provision_id in provision_ids
    ]
    events.append(_text_event(final_text, already_streamed=streamed))
    return events


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


def _estimated_usage_event_data(
    receipt: ContextReceipt,
    response: ChatResponse,
) -> dict:
    """Charge an estimate when the provider reported no usage at all.

    Streaming usage arrives only if the provider honours
    ``stream_options.include_usage``; plenty of OpenAI-compatible relays drop
    it, and then no TOKEN_USAGE event was emitted for the turn.  Sub-agent
    spend is metered from exactly those events, so both the per-agent and the
    per-batch token budgets silently stopped applying — a runaway was bounded
    only by the iteration cap and the wall-clock timeout.  An estimate is
    wrong in the last digit; no event is wrong by the whole budget.

    ``usage_reported`` stays 0: this is not a provider number, and the
    observability side must keep being able to tell the difference.
    """
    output_text = (response.text or "") + (response.reasoning or "")
    for call in response.tool_calls or ():
        output_text += json.dumps(
            getattr(call, "arguments", ""), ensure_ascii=False, default=str
        )
    input_tokens = receipt.estimated_input_tokens
    output_tokens = estimate_tokens(output_text)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        USAGE_REPORTED_KEY: 0,
        "estimated": True,
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
    # 逐条浅拷贝：prune/compress 会原地改写 content，
    # 不复制会污染调用方传入的 history dict（跨请求复用时留脏数据）。
    conversation = [dict(m) for m in messages]
    policy = ToolPolicy(
        tool_guard,
        fact_gate=fact_gate if fact_gate is not None else current_fact_gate(),
    )
    tool_runner = ToolRoundRunner(tool_registry, policy, hook_registry)
    tools = tool_runner.tools
    productive_iters = 0
    recovery_iters = 0
    preflight_iters = 0
    had_successful_tool = False
    incomplete_final_repairs = 0
    length_continuations = 0
    # (fragment, was_streamed) per fragment, not one flag for the join: a reply
    # can mix a non-streamed fallback fragment with a streamed continuation,
    # and a single flag then mislabels one of them — see _final_text_delivery.
    final_text_parts: list[tuple[str, bool]] = []
    active_provision_ids: list[str] = []
    context_recoveries = 0
    model_compaction_enabled = True
    compaction_failures = 0

    while productive_iters < max_iters:
        conversation = trim_conversation_to_message_cap(conversation)

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
            # 闩锁而不是每轮重试：真正压不动的对话（history 为空）重试无用。
            # 但一次超时/限流/被截断的摘要也会走到这里，单次即永久关停会让整
            # 个 run 只剩删除式裁剪 —— 瞬时故障给一次重试再闩锁。
            compaction_failures += 1
            model_compaction_enabled = compaction_failures < MAX_COMPACTION_FAILURES
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
        usage = _usage_event_data(response.usage) or _estimated_usage_event_data(
            fit.receipt, response
        )
        yield ExecutionEvent(EventType.TOKEN_USAGE, usage)
        yield _context_usage_event(
            fit.receipt,
            # Only a provider-reported count may pass as exact.  The estimated
            # fallback feeds back receipt.estimated_input_tokens, so handing it
            # over produced a positive int and flipped ``estimated`` to False —
            # the one field whose whole job is telling the two apart.
            input_tokens=(
                usage.get("input_tokens") if usage.get(USAGE_REPORTED_KEY) else None
            ),
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
                final_text_parts.append((response.text, response_text_was_streamed))
            final_text, final_text_streamed = _final_text_delivery(final_text_parts)

            if response.finish_reason == "length":
                if length_continuations < MAX_LENGTH_CONTINUATIONS:
                    length_continuations += 1
                    _append_length_continuation(
                        conversation, response.text, response.reasoning
                    )
                    continue
                if final_text:
                    for event in _final_text_events(
                        final_text,
                        streamed=final_text_streamed,
                        provision_ids=active_provision_ids,
                    ):
                        yield event
                    active_provision_ids.clear()
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
                    yield _text_event(final_text, already_streamed=final_text_streamed)
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
                    yield _text_event(final_text, already_streamed=final_text_streamed)
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
                # 只追加本轮分片：更早的分片已由 _append_length_continuation
                # 写进 conversation，重复追加会让同一段文本在修复提示里出现
                # 两次。这条路径现在真的够得到 —— INCOMPLETE_FINAL_MAX_CHARS
                # 放宽后，一段续写过的文本可以同时是假结尾，交互由
                # test_a_continued_answer_can_still_be_repaired_without_duplication 锁住。
                _append_incomplete_final_repair(
                    conversation, response.text, response.reasoning
                )
                final_text_parts.clear()
                continue
            if final_text:
                for event in _final_text_events(
                    final_text,
                    streamed=final_text_streamed,
                    provision_ids=active_provision_ids,
                ):
                    yield event
                active_provision_ids.clear()
            else:
                # A successful tool call is evidence gathering, not a valid
                # chat completion.  The caller still needs a final assistant
                # response to render and persist.
                yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "empty_model_response"})
            return

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

        async with aclosing(tool_runner.run(response.tool_calls)) as rounds:
            async for item in rounds:
                if isinstance(item, ToolRoundResult):
                    conversation.extend(item.messages)
                    tools = tool_runner.tools
                    had_successful_tool |= item.had_operational_success
                    round_had_success = item.had_success
                    round_had_failure = item.had_failure
                    round_had_preflight = item.had_preflight
                    if item.terminal:
                        return
                else:
                    yield item

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
