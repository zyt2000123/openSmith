"""Tests for unified TaskBudget (tokens / wall-clock / USD cost) enforcement."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from engine.context.fitting import ContextFitStatus, fit_request
from engine.execution.events import EventType
from engine.execution.orchestration.lifecycle import run_stream_with_runtime
from engine.execution.orchestration.runtime import EngineRequest, RuntimeContext, RuntimeServices
from engine.execution.react.react_loop import react_event_loop
from engine.execution.task_budget import (
    MIN_OUTPUT_TOKENS_DEFAULT,
    BatchTokenLedger,
    SubAgentScopedBudget,
    TaskBudget,
    TaskBudgetLimits,
    reserve_for_call_waiting,
    use_task_budget,
)
from engine.llm.adapters.anthropic import AnthropicAdapter
from engine.llm.adapters.openai import OpenAIAdapter
from engine.llm.contracts import (
    ChatResponse,
    LLMProviderConfig,
    LLMRequest,
    ModelLimits,
    ToolCallData,
)
from engine.llm.events import ProviderEvent, ProviderEventType
from engine.memory.maintenance import MemoryMaintenanceService
from engine.safety.approval import APPROVAL_BROKER, ApprovalScope, use_approval_context
from engine.safety.risk import RiskTier
from engine.safety.tool_guard import GuardResult, PermissionLevel
from engine.skill.registry import SkillRegistry
from engine.tool.registry import ToolRegistry


class _BudgetRecordingLLM:
    stream = False
    model = "claude-sonnet-4-20250514"
    context_window = 128_000
    context_window_declared = True
    max_output_tokens = 4_096
    max_output_tokens_declared = True
    limits = ModelLimits(128_000, True, 4_096, True)

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefix_cache_key: str | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
        })
        if not self.responses:
            return ChatResponse(text="done", finish_reason="stop")
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def _basic_registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def ping() -> str:
        return "pong"

    reg.register("ping", "Ping tool", {"type": "object", "properties": {}}, ping)
    return reg


def test_reserve_clamps_max_tokens_and_reconciles_actual_usage() -> None:
    budget = TaskBudget(TaskBudgetLimits(max_total_tokens=2000))
    check = budget.reserve_for_call(
        estimated_input_tokens=1500,
        desired_max_output_tokens=4096,
        model="test-model",
    )
    assert check.allowed
    assert check.reservation is not None
    assert check.reservation.reserved_output_tokens == 500
    assert budget.snapshot().in_flight_tokens == 2000

    snap = budget.reconcile(
        check.reservation,
        usage={"input_tokens": 1200, "output_tokens": 200, "total_tokens": 1400},
        model="test-model",
    )
    assert snap.in_flight_tokens == 0
    assert snap.spent_tokens == 1400
    assert not snap.exhausted


def _reserve(budget: Any, est: int = 5000, out: int = 32_000) -> Any:
    return budget.reserve_for_call(
        estimated_input_tokens=est,
        desired_max_output_tokens=out,
        model="test-model",
    )


def test_sibling_in_flight_holds_contend_without_tripping_turn_budget() -> None:
    budget = TaskBudget(TaskBudgetLimits(max_total_tokens=100_000))
    held = [_reserve(budget) for _ in range(3)]
    assert all(check.allowed for check in held)

    fourth = _reserve(budget)
    assert not fourth.allowed
    assert fourth.contended
    assert not budget.exhausted
    assert not budget.cancel_event.is_set()

    budget.reconcile(held[0].reservation, usage={"total_tokens": 6000}, model="test-model")
    assert _reserve(budget).allowed

    # A shortfall caused by actual spend still trips permanently.
    spent = TaskBudget(TaskBudgetLimits(max_total_tokens=1000))
    first = _reserve(spent, est=100, out=200)
    spent.reconcile(first.reservation, usage={"total_tokens": 950}, model="test-model")
    assert not _reserve(spent, est=100, out=200).contended
    assert spent.exhausted


def test_batch_ledger_contention_does_not_exhaust_batch() -> None:
    ledger = BatchTokenLedger(10_000)
    assert ledger.reserve("a", 1000, 9000, 128) == 9000
    assert ledger.reserve("b", 1000, 9000, 128) == BatchTokenLedger.BATCH_CONTENDED
    assert not ledger.exhausted
    ledger.reconcile("a", 1500)
    assert ledger.reserve("b", 1000, 9000, 128) == 7500


def test_subagent_narrows_parent_hold_to_its_own_cap() -> None:
    parent = TaskBudget(TaskBudgetLimits(max_total_tokens=100_000))
    scoped = SubAgentScopedBudget(
        agent_token_budget=8000,
        batch_ledger=BatchTokenLedger(600_000),
        task_timeout_seconds=600,
        parent=parent,
    )
    check = _reserve(scoped)
    assert check.allowed
    assert check.reservation.reserved_output_tokens == 3000
    assert parent.snapshot().in_flight_tokens == 8000


@pytest.mark.asyncio
async def test_parallel_subagents_wait_for_sibling_settlement() -> None:
    parent = TaskBudget(TaskBudgetLimits(max_total_tokens=100_000))
    batch = BatchTokenLedger(600_000)

    def scoped() -> SubAgentScopedBudget:
        return SubAgentScopedBudget(
            agent_token_budget=400_000,
            batch_ledger=batch,
            task_timeout_seconds=30,
            parent=parent,
        )

    holders = [scoped() for _ in range(3)]
    held = [_reserve(h) for h in holders]
    assert all(check.allowed for check in held)

    waiter = asyncio.create_task(
        reserve_for_call_waiting(
            scoped(),
            estimated_input_tokens=5000,
            desired_max_output_tokens=32_000,
            model="test-model",
        )
    )
    await asyncio.sleep(0.12)
    assert not waiter.done()
    holders[0].reconcile(held[0].reservation, usage={"total_tokens": 6000}, model="test-model")

    check = await asyncio.wait_for(waiter, timeout=2)
    assert check.allowed
    assert not parent.exhausted


@pytest.mark.asyncio
async def test_rejects_call_when_remaining_below_min_output_without_wrapup() -> None:
    budget = TaskBudget(TaskBudgetLimits(max_total_tokens=100))
    # Pre-spend to leave less than MIN_OUTPUT_TOKENS_DEFAULT after input.
    llm = _BudgetRecordingLLM([ChatResponse(text="never called", finish_reason="stop")])

    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "hello world " * 40}],
            _basic_registry(),
            task_budget=budget,
        )
    ]
    assert len(llm.calls) == 0, "must not make any paid provider call when below min output"
    incomplete = [ev for ev in events if ev.type is EventType.INCOMPLETE]
    assert len(incomplete) == 1
    assert incomplete[0].data["reason"] == "budget_tokens_exhausted"
    assert budget.exhausted
    assert budget.cancel_event.is_set()
    assert MIN_OUTPUT_TOKENS_DEFAULT == 128


@pytest.mark.asyncio
async def test_wall_clock_exhaustion_cancels_stream_and_retracts_provisional() -> None:
    class SlowStreamingLLM:
        stream = True
        model = "slow-model"
        context_window = 128_000
        context_window_declared = True
        max_output_tokens = 4_096
        max_output_tokens_declared = True
        limits = ModelLimits(128_000, True, 4_096, True)

        def __init__(self) -> None:
            self.fallback_calls = 0

        async def chat_events(self, messages, tools=None, prefix_cache_key=None, *, max_tokens=None):
            yield ProviderEvent(ProviderEventType.OUTPUT_TEXT_DELTA, {"delta": "partial draft"})
            await asyncio.sleep(5.0)
            yield ProviderEvent(ProviderEventType.RESPONSE_COMPLETED, {"finish_reason": "stop"})

        async def chat(self, messages, tools=None, prefix_cache_key=None, *, max_tokens=None):
            self.fallback_calls += 1
            return ChatResponse(text="fallback must not run", finish_reason="stop")

    llm = SlowStreamingLLM()
    budget = TaskBudget(TaskBudgetLimits(max_wall_seconds=0.8))
    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "say hello"}],
            _basic_registry(),
            task_budget=budget,
        )
    ]

    assert llm.fallback_calls == 0
    retracts = [ev for ev in events if ev.type is EventType.PROVISIONAL_RETRACT]
    assert retracts, "partial streamed draft must be retracted on wall-clock timeout"
    assert retracts[0].data["reason"] == "budget_wall_time_exhausted"
    incomplete = [ev for ev in events if ev.type is EventType.INCOMPLETE]
    assert incomplete and incomplete[0].data["reason"] == "budget_wall_time_exhausted"
    assert budget.snapshot().estimated_tokens > 0


def test_cost_limit_uses_local_price_table_and_respects_unpriced_policy() -> None:
    prices = {
        "claude-sonnet-4": {
            "input": 3.0,
            "output": 15.0,
            "cache_read": 0.3,
            "cache_write": 3.75,
        }
    }
    # Priced model with date suffix matches base entry.
    budget = TaskBudget(TaskBudgetLimits(max_cost_usd=0.05), price_table=prices)
    check = budget.reserve_for_call(
        estimated_input_tokens=1000,
        desired_max_output_tokens=4096,
        model="claude-sonnet-4-20250514",
    )
    assert check.allowed and check.reservation is not None
    # $0.05 - $0.003 input = $0.047 -> at $15/1M output -> 3133 tokens (< 4096).
    assert check.reservation.reserved_output_tokens < 4096
    assert check.reservation.reserved_output_tokens == 3133

    snap = budget.reconcile(
        check.reservation,
        usage={"input_tokens": 1000, "output_tokens": 4000, "total_tokens": 5000},
        model="claude-sonnet-4-20250514",
    )
    assert snap.exhausted
    assert snap.exhausted_dimension == "cost_usd"
    assert snap.stop_reason == "budget_cost_exhausted"

    # Unpriced model under warn_only: allowed, cost_unpriced=True, spent_cost_usd=None.
    warn_budget = TaskBudget(
        TaskBudgetLimits(max_cost_usd=1.0, unpriced_cost_policy="warn_only"),
        price_table=prices,
    )
    warn_check = warn_budget.reserve_for_call(
        estimated_input_tokens=500,
        desired_max_output_tokens=1024,
        model="unknown-relay-model",
    )
    assert warn_check.allowed
    warn_snap = warn_budget.reconcile(
        warn_check.reservation,
        usage={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
        model="unknown-relay-model",
    )
    assert warn_snap.cost_unpriced is True
    assert warn_snap.spent_cost_usd is None
    assert not warn_snap.exhausted

    # Unpriced model under fail_closed: rejected before call.
    closed_budget = TaskBudget(
        TaskBudgetLimits(max_cost_usd=1.0, unpriced_cost_policy="fail_closed"),
        price_table=prices,
    )
    closed_check = closed_budget.reserve_for_call(
        estimated_input_tokens=500,
        desired_max_output_tokens=1024,
        model="unknown-relay-model",
    )
    assert not closed_check.allowed
    assert closed_check.exhausted_dimension == "cost_usd"
    assert closed_check.detail == "model_unpriced"


@pytest.mark.asyncio
async def test_relay_omitting_usage_still_enforces_token_budget_via_estimate() -> None:
    # Provider returns usage=None on every turn (common on OpenAI-compatible relays).
    llm = _BudgetRecordingLLM([
        ChatResponse(
            tool_calls=[ToolCallData(id=f"c{i}", name="ping", arguments={})],
            usage=None,
            finish_reason="tool_calls",
        )
        for i in range(20)
    ])
    budget = TaskBudget(TaskBudgetLimits(max_total_tokens=900))
    events = [
        ev
        async for ev in react_event_loop(
            llm,
            [{"role": "user", "content": "keep pinging " * 20}],
            _basic_registry(),
            task_budget=budget,
            max_iters=20,
        )
    ]

    assert len(llm.calls) < 10
    incomplete = [ev for ev in events if ev.type is EventType.INCOMPLETE]
    assert incomplete and incomplete[0].data["reason"] == "budget_tokens_exhausted"
    snap = budget.snapshot()
    assert snap.estimated_tokens > 0
    assert snap.spent_tokens >= snap.estimated_tokens


@pytest.mark.asyncio
async def test_budget_exhausted_skips_paid_compaction_and_paid_memory_compile(
    tmp_path: Path,
) -> None:
    class SmallWindowLLM(_BudgetRecordingLLM):
        context_window = 2_000
        max_output_tokens = 256
        limits = ModelLimits(2_000, True, 256, True)

    llm = SmallWindowLLM([])
    exhausted_budget = TaskBudget(TaskBudgetLimits(max_total_tokens=100))
    exhausted_budget.trip("tokens")

    big_history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old " + ("a" * 3000)},
        {"role": "assistant", "content": "old reply " + ("b" * 3000)},
        {"role": "user", "content": "current small request"},
    ]
    fit = await fit_request(
        big_history,
        None,
        llm,
        allow_model_compaction=True,
        task_budget=exhausted_budget,
    )
    assert len(llm.calls) == 0, "exhausted budget must skip paid LLM compaction"
    assert "compaction_skipped_budget" in fit.actions
    assert fit.status in {ContextFitStatus.RECOVERED, ContextFitStatus.FIT}

    # Memory maintenance on budget-exhausted incomplete turn must not invoke LLM.
    mem_llm = _BudgetRecordingLLM([])
    service = MemoryMaintenanceService(mem_llm, defer_maintenance=False)
    ok = await service.record_turn(
        tmp_path,
        "refactor everything",
        "stopped early",
        had_tools=True,
        turn_status="incomplete",
        turn_reason="budget_tokens_exhausted",
    )
    assert ok
    assert len(mem_llm.calls) == 0


def test_openai_and_anthropic_adapters_apply_request_max_tokens() -> None:
    openai_adapter = OpenAIAdapter(
        LLMProviderConfig(
            provider="openai",
            api_key="test-key",
            base_url="https://api.example.com/v1",
            model="gpt-4o",
            max_output_tokens=4096,
        )
    )
    req_clamped = LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=500)
    body = openai_adapter._request_body(req_clamped, stream=False)
    assert body["max_tokens"] == 500
    # Never raise above configured adapter ceiling.
    req_above = LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=99999)
    assert openai_adapter._request_body(req_above, stream=False)["max_tokens"] == 4096

    anthropic_adapter = AnthropicAdapter(
        LLMProviderConfig(
            provider="anthropic",
            api_key="test-key",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-4",
            max_output_tokens=4096,
        )
    )
    abody = anthropic_adapter._request_body(req_clamped, stream=False)
    assert abody["max_tokens"] == 500
    assert anthropic_adapter._request_body(req_above, stream=False)["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_approval_wait_clamped_by_wall_clock_budget() -> None:
    class ApprovalGuard:
        def check(self, call):
            return GuardResult(
                allowed=False,
                level=PermissionLevel.EXECUTE,
                reason="needs approval",
                approval_required=True,
                risk=RiskTier.HIGH,
                approval_scope=ApprovalScope.sandbox_command("echo hi"),
            )

    llm = _BudgetRecordingLLM([
        ChatResponse(
            tool_calls=[ToolCallData(id="a1", name="ping", arguments={})],
            usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            finish_reason="tool_calls",
        ),
    ])
    budget = TaskBudget(TaskBudgetLimits(max_wall_seconds=0.7))
    with use_approval_context(APPROVAL_BROKER, "run-wall-approval"):
        events = [
            ev
            async for ev in react_event_loop(
                llm,
                [{"role": "user", "content": "run ping"}],
                _basic_registry(),
                tool_guard=ApprovalGuard(),  # type: ignore[arg-type]
                task_budget=budget,
            )
        ]
    incomplete = [ev for ev in events if ev.type is EventType.INCOMPLETE]
    assert incomplete and incomplete[0].data["reason"] == "budget_wall_time_exhausted"


@pytest.mark.asyncio
async def test_lifecycle_propagates_request_budget_and_emits_run_finished_snapshot(
    tmp_path: Path,
) -> None:
    agents_dir = Path(__file__).resolve().parents[3] / "agents"
    llm = _BudgetRecordingLLM([
        ChatResponse(
            text="all done",
            usage={"input_tokens": 250, "output_tokens": 50, "total_tokens": 300},
            finish_reason="stop",
        )
    ])
    stream = run_stream_with_runtime(
        EngineRequest(
            message="hello",
            working_dir=str(tmp_path),
            ephemeral=True,
            budget=TaskBudgetLimits(max_total_tokens=10_000, max_wall_seconds=60.0),
        ),
        RuntimeContext(
            agent_id="smith",
            agent_name="Smith",
            profile_dir=tmp_path,
            agents_dir=agents_dir,
            session_id="sess-budget-1",
        ),
        RuntimeServices(
            llm=llm,
            tool_registry=_basic_registry(),
            skill_registry=SkillRegistry(),
            owns_llm_clients=False,
        ),
    )
    events = [ev async for ev in stream.stream_events()]
    budget_updates = [ev for ev in events if ev.type is EventType.BUDGET_UPDATE]
    assert budget_updates
    assert budget_updates[-1].data["tokens_used"] == 300
    assert budget_updates[-1].data["tokens_limit"] == 10_000
    finished = events[-1]
    assert finished.type is EventType.RUN_FINISHED
    assert finished.data["budget"]["spent_tokens"] == 300
    assert finished.data["budget"]["limits"]["max_total_tokens"] == 10_000
