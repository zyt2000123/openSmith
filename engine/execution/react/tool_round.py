"""Execute a serial tool batch, streaming events and owning its protocol messages."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator
from uuid import uuid4

from engine.context.compression import RUNTIME_USER_NOTE_PREFIX
from engine.execution.evidence import tool_result_hash
from engine.execution.events import EventType, ExecutionEvent
from engine.execution.runtime_control import repeated_success_prompt, tool_blocked_prompt
from engine.llm.contracts import ToolCallData
from engine.safety.approval import (
    ApprovalRequest, ApprovalTimeoutError, build_approval_presentation,
    current_approval_context, summarize_arguments,
)
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall
from .budget import (
    MAX_IDENTICAL_TOOL_ERRORS, REPEATED_SUCCESS_WARN_THRESHOLD,
    TOOL_FAILURE_BUDGET_MESSAGE, TOOL_FAILURE_HINT, budget_exhausted_message,
)
from .progress import ProgressLedger
from .smith_ui import smith_ui_fallback, validate_smith_ui_call

if TYPE_CHECKING:
    from engine.execution.hooks import HookRegistry
    from engine.tool.registry import ToolRegistry

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
    The actual volatile value is appended after all results as a user turn,
    so it never alters the stable system-prompt prefix. It carries the
    shared prefix so the context fitter can tell engine framing apart from the
    request being executed (see ``compression._split_active_context``).
    """
    if tool_name == _CURRENT_TIME_TOOL and not is_error:
        return [
            {"role": "tool", "tool_call_id": call_id, "content": _CURRENT_TIME_TOOL_ACK},
            {"role": "user", "content": f"{RUNTIME_USER_NOTE_PREFIX}{content}"},
        ]
    return [{"role": "tool", "tool_call_id": call_id, "content": content}]


@dataclass(frozen=True)
class ToolRoundResult:
    messages: tuple[dict, ...]
    had_success: bool
    had_failure: bool
    had_preflight: bool
    had_operational_success: bool
    terminal: bool


@dataclass
class _Batch:
    messages: list[dict] = field(default_factory=list)
    had_success: bool = False
    had_failure: bool = False
    had_preflight: bool = False
    had_operational_success: bool = False
    terminal: bool = False

    def finish(self) -> ToolRoundResult:
        # Runtime framing is committed only after every result in this batch.
        results = [m for m in self.messages if m.get("role") == "tool"]
        notes = [m for m in self.messages if m.get("role") != "tool"]
        return ToolRoundResult(
            tuple(results + notes), self.had_success, self.had_failure,
            self.had_preflight, self.had_operational_success, self.terminal,
        )


class ToolRoundRunner:
    """One run's tool capabilities and failure/progress state; no model history."""

    def __init__(self, tool_registry: "ToolRegistry", policy: ToolPolicy,
                 hook_registry: "HookRegistry | None" = None) -> None:
        self.tool_registry = tool_registry
        self.policy = policy
        self.hook_registry = hook_registry
        self.consecutive_errors = 0
        self.progress = ProgressLedger(repeat_threshold=REPEATED_SUCCESS_WARN_THRESHOLD)
        self._running = False
        self.lazy_tool_schemas = bool(getattr(tool_registry, "lazy_tool_schemas", False))
        if self.lazy_tool_schemas:
            # Tool descriptions live in the stable prompt prefix. The provider sees
            # only this tiny loader schema until the model explicitly asks for one
            # capability, keeping the large JSON contracts out of every initial turn.
            self.tools: list[dict] | None = (
                [_tool_schema_loader_definition()] if tool_registry.list_tools() else None
            )
        else:
            self.tools = tool_registry.get_schemas() or None
        self.visible_tool_names = {
            schema["function"]["name"]
            for schema in (self.tools or [])
            if isinstance(schema.get("function"), dict)
            and isinstance(schema["function"].get("name"), str)
        }

    async def run(self, calls: list[ToolCallData]) -> AsyncIterator[ExecutionEvent | ToolRoundResult]:
        if self._running:
            raise RuntimeError("A tool round is already running")
        self._running = True
        batch = _Batch()
        try:
            self.policy.begin_round()
            async for event in self._execute(calls, batch):
                if event.type == EventType.INCOMPLETE:
                    batch.terminal = True
                yield event
            if batch.had_failure:
                self.progress.break_sequence()
            yield batch.finish()
        finally:
            self._running = False

    async def _execute(self, calls: list[ToolCallData], batch: _Batch) -> AsyncIterator[ExecutionEvent]:
        for tc in calls:
            if self.lazy_tool_schemas and tc.name == _TOOL_SCHEMA_LOADER:
                requested_name, schema, error = _requested_tool_schema(
                    self.tool_registry, tc.arguments,
                )
                content = (
                    json.dumps(schema, ensure_ascii=False, sort_keys=True)
                    if schema is not None
                    else f"Error: {error}"
                )
                batch.messages.append({
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
                    batch.had_failure = True
                    self.consecutive_errors += 1
                else:
                    assert requested_name is not None
                    self.visible_tool_names.add(requested_name)
                    if self.tools is None:
                        self.tools = [_tool_schema_loader_definition(), schema]
                    elif not any(
                        isinstance(candidate.get("function"), dict)
                        and candidate["function"].get("name") == requested_name
                        for candidate in self.tools
                    ):
                        # Provider adapters may retain the argument list for
                        # diagnostics; replace rather than mutate it so a past
                        # request remains an accurate snapshot of its tools.
                        self.tools = [*self.tools, schema]
                    # Loading a schema consumes an iteration, but is not an
                    # evidence-gathering tool success: a following text answer
                    # is still valid even when no operational tool ran.
                    batch.had_success = True
                    self.consecutive_errors = 0
                continue

            call = self.tool_registry.normalize_call(
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
            if self.lazy_tool_schemas and call.name not in self.visible_tool_names:
                loaded_name, loaded_schema, _ = _requested_tool_schema(
                    self.tool_registry, {"name": call.name},
                )
                if loaded_schema is not None and loaded_name is not None:
                    self.visible_tool_names.add(loaded_name)
                    self.tools = [*(self.tools or []), loaded_schema]

            # ``render_ui`` is an engine-owned presentation capability. It is
            # deliberately not executed like a provider tool: rendering it
            # cannot write files, call the network, or make arbitrary JSON a
            # client-side component tree. The validated event is sent only to
            # clients that explicitly understand the smith-ui contract.
            if call.name == "render_ui":
                if call.name not in self.visible_tool_names:
                    batch.messages.append({
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
                    batch.had_failure = True
                    self.consecutive_errors += 1
                    continue

                decision = self.policy.evaluate(call)
                if not decision.allowed:
                    batch.messages.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
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
                    batch.had_failure = True
                    self.consecutive_errors += 1
                    continue

                validated = validate_smith_ui_call(
                    call.arguments,
                    working_dir=self.tool_registry.working_directory,
                )
                if not validated.ok or validated.payload is None:
                    reason = validated.reason or "Invalid smith-ui payload"
                    batch.messages.append({"role": "tool", "tool_call_id": call.id, "content": f"Error: {reason}"})
                    yield ExecutionEvent(EventType.SMITH_UI_FALLBACK, smith_ui_fallback(call.arguments, reason))
                    batch.had_failure = True
                    self.consecutive_errors += 1
                    continue

                batch.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "Smith UI rendered successfully. Do not repeat its contents as raw JSON.",
                })
                yield ExecutionEvent(EventType.SMITH_UI, validated.payload)
                batch.had_success = True
                batch.had_operational_success = True
                self.consecutive_errors = 0
                self.progress.reset_failures()
                continue

            # A node-scoped registry deliberately hides capabilities that are
            # not part of this stage.  Reject a hallucinated or disabled call
            # before ToolPolicy sees it: a capability that cannot execute must
            # never turn into an approval request merely because it exists in
            # the broader identity-level registry.
            if call.name not in self.visible_tool_names:
                unavailable_message = getattr(
                    self.tool_registry,
                    "unavailable_tool_message",
                    None,
                )
                content = (
                    unavailable_message(call.name)
                    if callable(unavailable_message)
                    else f"Tool disabled: {call.name}"
                )
                batch.messages.append({
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
                batch.had_failure = True
                self.consecutive_errors += 1
                if self.progress.record_failure(f"{tc.name}:{content[:120]}") >= MAX_IDENTICAL_TOOL_ERRORS:
                    yield ExecutionEvent(
                        EventType.TEXT_DELTA,
                        {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                    )
                    yield ExecutionEvent(EventType.INCOMPLETE, {
                        "reason": "identical_tool_error_loop",
                    })
                    return
                if self.consecutive_errors >= 3:
                    batch.messages.append({"role": "system", "content": TOOL_FAILURE_HINT})
                    self.consecutive_errors = 0
                continue

            granted_approval_id: str | None = None
            yield ExecutionEvent(EventType.TOOL_CALL_START, {"name": tc.name, "id": tc.id, "arguments": call.arguments})

            decision = self.policy.evaluate(call)
            if not decision.allowed:
                if decision.challenged:
                    batch.messages.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                    yield ExecutionEvent(EventType.TOOL_CALL_RESULT, {
                        "id": tc.id,
                        "error": False,
                        "blocked": False,
                        "preflight": True,
                        "reason": decision.reason,
                        "level": decision.level.value,
                    })
                    batch.had_preflight = True
                    continue
                if decision.approval_required:
                    approval_context = current_approval_context()
                    if approval_context is not None:
                        broker, run_id = approval_context
                        arguments_summary = summarize_arguments(call.arguments)
                        definition = self.tool_registry.definitions().get(call.name)
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
                            batch.messages.append({
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": f"[BLOCKED] {denial}",
                            })
                            batch.messages.append({"role": "system", "content": tool_blocked_prompt()})
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
                            batch.had_failure = True
                            self.consecutive_errors += 1
                            # 没有拒绝记忆：模型重发同一调用就再弹一次审批窗、
                            # 再等最长 300 秒。"Approval timed out" 读起来像瞬时
                            # 故障，天然诱导重试 —— 无人值守时整个 run 就这么挂
                            # 到恢复上限。同错熔断和其它三条拒绝路径一致。
                            if self.progress.record_failure(
                                f"{tc.name}:{denial[:120]}"
                            ) >= MAX_IDENTICAL_TOOL_ERRORS:
                                yield ExecutionEvent(
                                    EventType.TEXT_DELTA,
                                    {"text": budget_exhausted_message(
                                        TOOL_FAILURE_BUDGET_MESSAGE
                                    )},
                                )
                                yield ExecutionEvent(EventType.INCOMPLETE, {
                                    "reason": "identical_tool_error_loop",
                                })
                                return
                            continue
                    else:
                        batch.messages.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                        batch.messages.append({"role": "system", "content": tool_blocked_prompt()})
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
                        batch.had_failure = True
                        self.consecutive_errors += 1
                        continue
                else:
                    batch.messages.append({"role": "tool", "tool_call_id": call.id, "content": decision.observation})
                    batch.messages.append({"role": "system", "content": tool_blocked_prompt()})
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
                    batch.had_failure = True
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= 3:
                        batch.messages.append({"role": "system", "content": TOOL_FAILURE_HINT})
                        self.consecutive_errors = 0
                    continue

            # ========== PreToolUse Hook 检查 ==========
            if self.hook_registry is not None:
                allowed, denial_reason = await self.hook_registry.run_pre_hooks(
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
                    batch.messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"[BLOCKED] {denial}",
                    })
                    batch.messages.append({"role": "system", "content": tool_blocked_prompt()})
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
                    batch.had_failure = True
                    self.consecutive_errors += 1
                    # A hook that denies the same call every round is a loop the
                    # other error paths already bound; without this the model can
                    # retry a permanently-blocked edit until the whole iteration
                    # budget is gone.
                    if self.progress.record_failure(f"{tc.name}:{denial[:120]}") >= MAX_IDENTICAL_TOOL_ERRORS:
                        yield ExecutionEvent(
                            EventType.TEXT_DELTA,
                            {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                        )
                        yield ExecutionEvent(EventType.INCOMPLETE, {
                            "reason": "identical_tool_error_loop",
                        })
                        return
                    if self.consecutive_errors >= 3:
                        batch.messages.append({"role": "system", "content": TOOL_FAILURE_HINT})
                        self.consecutive_errors = 0
                    continue

            with self.tool_registry.authorize_execution(
                call,
                approval_id=granted_approval_id,
                approval_scope=decision.approval_scope,
            ):
                result = await self.tool_registry.execute(call)

            # ========== PostToolUse Hook 检查 ==========
            if self.hook_registry is not None:
                warnings = await self.hook_registry.run_post_hooks(
                    call.name,
                    call.arguments,
                    result
                )
                if warnings:
                    # 将警告注入到对话中（作为 system 消息）
                    warning_text = "\n\n".join(warnings)
                    batch.messages.append({"role": "system", "content": warning_text})

            batch.messages.extend(_tool_result_messages(
                call.name, result.call_id, result.content, is_error=result.is_error,
            ))
            result_event = {
                "id": tc.id,
                "name": call.name,
                "error": result.is_error,
                "blocked": False,
                "preflight": False,
                # Preserve the existing audit hash over the model-visible result.
                # Progress uses output.raw_digest, computed before truncation.
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
                batch.had_failure = True
                self.consecutive_errors += 1
                if self.progress.record_failure(f"{tc.name}:{result.content[:120]}") >= MAX_IDENTICAL_TOOL_ERRORS:
                    yield ExecutionEvent(
                        EventType.TEXT_DELTA,
                        {"text": budget_exhausted_message(TOOL_FAILURE_BUDGET_MESSAGE)},
                    )
                    yield ExecutionEvent(EventType.INCOMPLETE, {"reason": "identical_tool_error_loop"})
                    return
                if self.consecutive_errors >= 3:
                    batch.messages.append({"role": "system", "content": TOOL_FAILURE_HINT})
                    self.consecutive_errors = 0
            else:
                batch.had_success = True
                batch.had_operational_success = True
                self.consecutive_errors = 0
                self.progress.reset_failures()
                output = result.metadata.get("output")
                digest = output.get("raw_digest") if isinstance(output, dict) else None
                if not isinstance(digest, str):
                    # Older replay entries contain only their bounded stored
                    # content. Do not label its digest as a raw output digest.
                    digest = hashlib.sha256(result.content.encode("utf-8", "replace")).hexdigest()
                observation = self.progress.observe(call.name, call.arguments, digest)
                if observation.warn:
                    content = (
                        f"The last {observation.period}-tool sequence repeated three times "
                        "with unchanged results. Use the existing evidence or change approach."
                        if observation.kind == "cycle"
                        else repeated_success_prompt(call.name, observation.count)
                    )
                    batch.messages.append({"role": "system", "content": content})

