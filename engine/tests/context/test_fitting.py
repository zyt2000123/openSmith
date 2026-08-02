from __future__ import annotations

import asyncio

from engine.context import ContextFitStatus, fit_request
from engine.context.budget import estimate_messages_tokens, estimate_tokens
from engine.llm.contracts import ModelLimits


class _BoundedLLM:
    limits = ModelLimits(
        context_window=8_192,
        context_window_declared=True,
        max_output_tokens=2_048,
        max_output_tokens_declared=True,
    )


class _CompactingLLM:
    limits = ModelLimits(
        context_window=4_096,
        context_window_declared=True,
        max_output_tokens=1_024,
        max_output_tokens_declared=True,
    )

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[dict]] = []
        self.fail = fail

    async def chat(self, messages, tools=None):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("summary unavailable")
        return type("Response", (), {
            "text": (
                "<context_summary><conversation_overview>overview</conversation_overview>"
                "<key_knowledge>knowledge</key_knowledge>"
                "<file_system_state>files</file_system_state>"
                "<recent_actions>actions</recent_actions>"
                "<current_plan>plan</current_plan></context_summary>"
            ),
            "finish_reason": "stop",
        })()


def test_token_estimate_conservatively_bounds_cjk_byte_fallback() -> None:
    assert estimate_tokens("一") == 3
    assert estimate_tokens("一a") == 4
    assert estimate_tokens("abc") == 1


def test_fit_request_counts_tool_schemas_before_calling_the_provider() -> None:
    result = asyncio.run(fit_request(
        [{"role": "user", "content": "hello"}],
        [{
            "type": "function",
            "function": {
                "name": "oversized",
                "description": "证" * 8_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        _BoundedLLM(),
    ))

    assert result.status is ContextFitStatus.UNFIT_TOOL_SCHEMAS
    assert result.receipt.tool_schema_tokens >= 8_000
    assert result.receipt.estimated_input_tokens > result.receipt.safe_input_budget


def test_fit_request_counts_assistant_tool_call_arguments() -> None:
    small = asyncio.run(fit_request(
        [{"role": "assistant", "content": "", "tool_calls": []}],
        None,
        _BoundedLLM(),
    ))
    large = asyncio.run(fit_request(
        [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "search", "arguments": "证" * 2_000},
            }],
        }],
        None,
        _BoundedLLM(),
    ))

    assert large.receipt.message_tokens > small.receipt.message_tokens + 1_900


def test_fit_request_compacts_and_remeasures_before_returning() -> None:
    llm = _CompactingLLM()
    system = {"role": "system", "content": "protected system contract"}
    active_request = {"role": "user", "content": "Continue the investigation safely."}

    result = asyncio.run(fit_request(
        [
            system,
            {"role": "user", "content": "证" * 4_000},
            {"role": "assistant", "content": "Earlier investigation."},
            active_request,
        ],
        None,
        llm,
    ))

    assert result.status is ContextFitStatus.COMPACTED
    assert result.fits
    assert len(llm.calls) == 1
    assert result.messages[0] == system
    assert result.messages[-1] == active_request
    assert result.receipt.estimated_input_tokens <= result.receipt.safe_input_budget
    assert estimate_messages_tokens(llm.calls[0]) <= result.receipt.safe_input_budget


def test_fit_request_uses_deterministic_recovery_when_compaction_fails() -> None:
    llm = _CompactingLLM(fail=True)
    system = {"role": "system", "content": "protected system contract"}
    active_request = {"role": "user", "content": "Continue the investigation safely."}

    result = asyncio.run(fit_request(
        [
            system,
            {"role": "user", "content": "证" * 4_000},
            {"role": "assistant", "content": "Earlier investigation."},
            active_request,
        ],
        None,
        llm,
    ))

    assert result.status is ContextFitStatus.RECOVERED
    assert result.fits
    assert result.messages[0] == system
    assert result.messages[-1] == active_request
    assert result.receipt.estimated_input_tokens <= result.receipt.safe_input_budget
    assert "compaction_failed" in result.actions


def test_fit_request_rejects_oversized_static_prompt_without_provider_call() -> None:
    llm = _CompactingLLM()

    result = asyncio.run(fit_request(
        [
            {"role": "system", "content": "证" * 3_000},
            {"role": "user", "content": "hello"},
        ],
        None,
        llm,
    ))

    assert result.status is ContextFitStatus.UNFIT_STATIC_PROMPT
    assert not result.fits
    assert llm.calls == []


def test_fit_request_treats_all_leading_system_messages_as_protected() -> None:
    llm = _CompactingLLM()
    contracts = [
        {"role": "system", "content": "证" * 1_400},
        {"role": "system", "content": "证" * 1_400},
    ]

    result = asyncio.run(fit_request(
        [*contracts, {"role": "user", "content": "hello"}],
        None,
        llm,
    ))

    assert result.status is ContextFitStatus.UNFIT_STATIC_PROMPT
    assert result.messages[:2] == tuple(contracts)
    assert llm.calls == []


def test_fit_request_rejects_an_oversized_active_user_turn_without_summarizing() -> None:
    """A current request must never be silently replaced by a history summary."""
    llm = _CompactingLLM()
    active_request = (
        "x" * 8_000
        + "\nCRITICAL CONSTRAINT: never deploy or modify production."
    )
    messages = [
        {"role": "system", "content": "protected system contract"},
        {"role": "user", "content": active_request},
    ]

    result = asyncio.run(fit_request(messages, None, llm))

    assert result.status is ContextFitStatus.UNFIT_REQUEST
    assert result.messages == tuple(messages)
    assert llm.calls == []


def test_deterministic_recovery_preserves_the_active_user_turn_verbatim() -> None:
    """A failed summary may trim history, never the request being executed."""
    llm = _CompactingLLM(fail=True)
    active_request = "Investigate the failure but never deploy or modify production."

    result = asyncio.run(fit_request(
        [
            {"role": "system", "content": "protected system contract"},
            {"role": "user", "content": "x" * 8_000},
            {"role": "assistant", "content": "Earlier investigation."},
            {"role": "user", "content": active_request},
        ],
        None,
        llm,
    ))

    assert result.status is ContextFitStatus.RECOVERED
    assert result.messages[-1] == {"role": "user", "content": active_request}
    assert "never deploy or modify production" in str(result.messages)
