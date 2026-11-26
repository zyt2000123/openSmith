"""Unified single-turn hard budget enforcement (tokens, wall-clock, USD cost).

``TaskBudget`` meters every LLM call in one Agent turn — main ReAct loop,
SkillChain nodes, LLM compaction, and concurrent sub-agent fan-outs — using
pre-call reservation, dynamic ``max_tokens`` clamping, post-call reconciliation,
and zero-wrap-up cooperative cancellation when any ceiling is reached.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Literal
from uuid import uuid4

from engine.llm.usage import USAGE_REPORTED_KEY, normalize_usage

BudgetDimension = Literal["tokens", "wall_seconds", "cost_usd"]
UnpricedCostPolicy = Literal["warn_only", "fail_closed"]

MIN_OUTPUT_TOKENS_DEFAULT = 128
MIN_OUTPUT_TOKENS_THINKING = 1024
MIN_WALL_SECONDS_FOR_CALL = 0.5

_DATE_SUFFIX_RE = re.compile(r"-\d{4}-?\d{2}-?\d{2}$|-\d{8}$")

_STOP_REASON_BY_DIMENSION: Mapping[BudgetDimension, str] = {
    "tokens": "budget_tokens_exhausted",
    "wall_seconds": "budget_wall_time_exhausted",
    "cost_usd": "budget_cost_exhausted",
}


def stop_reason_for_dimension(dimension: BudgetDimension) -> str:
    return _STOP_REASON_BY_DIMENSION[dimension]


def _lookup_price(
    price_table: Mapping[str, Mapping[str, float]],
    model: str,
) -> Mapping[str, float] | None:
    if not model or not price_table:
        return None
    if model in price_table:
        return price_table[model]
    lowered = model.casefold()
    for key, entry in price_table.items():
        if key.casefold() == lowered:
            return entry
    stripped = _DATE_SUFFIX_RE.sub("", lowered)
    if stripped != lowered:
        for key, entry in price_table.items():
            if _DATE_SUFFIX_RE.sub("", key.casefold()) == stripped:
                return entry
    return None


def _compute_cost_usd(
    price_entry: Mapping[str, float],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p_in = float(price_entry.get("input", 0.0))
    p_out = float(price_entry.get("output", 0.0))
    p_cache_read = float(price_entry.get("cache_read", p_in))
    p_cache_write = float(price_entry.get("cache_write", p_in))
    uncached_input = max(0, input_tokens - cache_read_tokens)
    return (
        uncached_input * p_in
        + cache_read_tokens * p_cache_read
        + cache_write_tokens * p_cache_write
        + output_tokens * p_out
    ) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class TaskBudgetLimits:
    """Hard per-turn resource ceilings."""

    max_total_tokens: int | None = None
    max_wall_seconds: float | None = None
    max_cost_usd: float | None = None
    unpriced_cost_policy: UnpricedCostPolicy = "warn_only"

    @property
    def has_limits(self) -> bool:
        return (
            self.max_total_tokens is not None
            or self.max_wall_seconds is not None
            or self.max_cost_usd is not None
        )

    def tighten(self, override: "TaskBudgetLimits | None") -> "TaskBudgetLimits":
        """Apply a request-level override that may only tighten configured ceilings."""
        if override is None:
            return self

        def _min_opt_int(base: int | None, req: int | None) -> int | None:
            if base is None:
                return req
            if req is None:
                return base
            return min(base, req)

        def _min_opt_float(base: float | None, req: float | None) -> float | None:
            if base is None:
                return req
            if req is None:
                return base
            return min(base, req)

        policy: UnpricedCostPolicy = (
            "fail_closed"
            if self.unpriced_cost_policy == "fail_closed"
            or override.unpriced_cost_policy == "fail_closed"
            else "warn_only"
        )
        return TaskBudgetLimits(
            max_total_tokens=_min_opt_int(self.max_total_tokens, override.max_total_tokens),
            max_wall_seconds=_min_opt_float(self.max_wall_seconds, override.max_wall_seconds),
            max_cost_usd=_min_opt_float(self.max_cost_usd, override.max_cost_usd),
            unpriced_cost_policy=policy,
        )

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "max_total_tokens": self.max_total_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "max_cost_usd": self.max_cost_usd,
            "unpriced_cost_policy": self.unpriced_cost_policy,
        }


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """In-flight resource reservation for a single provider call."""

    reservation_id: str
    model: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    reserved_tokens: int
    reserved_cost_usd: float
    timeout_seconds: float | None
    is_subagent: bool = False
    pricing_available: bool = False
    parent_reservation: "BudgetReservation | None" = None


@dataclass(frozen=True, slots=True)
class BudgetCheckResult:
    """Result of attempting to reserve budget before an LLM call."""

    allowed: bool
    reservation: BudgetReservation | None = None
    exhausted_dimension: BudgetDimension | None = None
    stop_reason: str | None = None
    detail: str | None = None
    # Refused only because concurrent in-flight reservations hold the headroom;
    # actual spend still leaves room, so the budget is not tripped and the call
    # may be retried once a sibling reservation settles.
    contended: bool = False


@dataclass(frozen=True, slots=True)
class TaskBudgetSnapshot:
    """Point-in-time immutable view of turn budget consumption."""

    limits: dict[str, int | float | str | None]
    spent_tokens: int
    input_tokens: int
    output_tokens: int
    subagent_tokens: int
    estimated_tokens: int
    in_flight_tokens: int
    wall_seconds: float
    spent_cost_usd: float | None
    subagent_cost_usd: float | None
    cost_unpriced: bool
    unpriced_models: tuple[str, ...]
    exhausted: bool
    exhausted_dimension: BudgetDimension | None
    stop_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": dict(self.limits),
            "spent_tokens": self.spent_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "subagent_tokens": self.subagent_tokens,
            "estimated_tokens": self.estimated_tokens,
            "in_flight_tokens": self.in_flight_tokens,
            "wall_seconds": round(self.wall_seconds, 2),
            "spent_cost_usd": (
                round(self.spent_cost_usd, 6)
                if self.spent_cost_usd is not None
                else None
            ),
            "subagent_cost_usd": (
                round(self.subagent_cost_usd, 6)
                if self.subagent_cost_usd is not None
                else None
            ),
            "cost_unpriced": self.cost_unpriced,
            "unpriced_models": list(self.unpriced_models),
            "exhausted": self.exhausted,
            "exhausted_dimension": self.exhausted_dimension,
            "stop_reason": self.stop_reason,
        }

    def to_update_event_data(self) -> dict[str, Any]:
        return {
            "tokens_used": self.spent_tokens,
            "tokens_limit": self.limits.get("max_total_tokens"),
            "subagent_tokens": self.subagent_tokens,
            "estimated_tokens": self.estimated_tokens,
            "wall_seconds": round(self.wall_seconds, 2),
            "wall_limit": self.limits.get("max_wall_seconds"),
            "cost_usd": (
                round(self.spent_cost_usd, 6)
                if self.spent_cost_usd is not None
                else None
            ),
            "cost_limit": self.limits.get("max_cost_usd"),
            "cost_unpriced": self.cost_unpriced,
            "exhausted_dimension": self.exhausted_dimension,
        }


class TaskBudget:
    """Shared root budget for one Agent turn (main loop, compaction, sub-agents)."""

    def __init__(
        self,
        limits: TaskBudgetLimits | None = None,
        *,
        price_table: Mapping[str, Mapping[str, float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or TaskBudgetLimits()
        self._price_table: dict[str, dict[str, float]] = {
            str(model): {str(k): float(v) for k, v in entry.items()}
            for model, entry in (price_table or {}).items()
            if isinstance(entry, Mapping)
        }
        self._clock = clock
        self._started_at: float = clock()
        self._lock = threading.RLock()
        self.cancel_event = asyncio.Event()

        self._spent_tokens: int = 0
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._subagent_tokens: int = 0
        self._estimated_tokens: int = 0
        self._in_flight_tokens: int = 0

        self._spent_cost_usd: float = 0.0
        self._subagent_cost_usd: float = 0.0
        self._in_flight_cost_usd: float = 0.0
        self._has_priced_usage: bool = False
        self._has_subagent_priced_usage: bool = False
        self._unpriced_models: set[str] = set()

        self._active_reservations: dict[str, BudgetReservation] = {}
        self._exhausted: bool = False
        self._exhausted_dimension: BudgetDimension | None = None
        self._stop_reason: str | None = None
        self._exhausted_detail: str | None = None

    @property
    def has_limits(self) -> bool:
        return self.limits.has_limits

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._exhausted

    @property
    def exhausted_dimension(self) -> BudgetDimension | None:
        with self._lock:
            return self._exhausted_dimension

    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    def remaining_wall_seconds(self) -> float | None:
        if self.limits.max_wall_seconds is None:
            return None
        return max(0.0, self.limits.max_wall_seconds - self.elapsed_seconds())

    def check_wall_clock(self) -> bool:
        """Return False and trip wall_seconds if the wall-clock deadline passed."""
        with self._lock:
            if self._exhausted:
                return False
            if self.limits.max_wall_seconds is None:
                return True
            if self.elapsed_seconds() >= self.limits.max_wall_seconds:
                self._trip_locked("wall_seconds")
                return False
            return True

    def _trip_locked(
        self,
        dimension: BudgetDimension,
        *,
        detail: str | None = None,
    ) -> None:
        if not self._exhausted:
            self._exhausted = True
            self._exhausted_dimension = dimension
            self._stop_reason = stop_reason_for_dimension(dimension)
            self._exhausted_detail = detail
        self.cancel_event.set()

    def trip(self, dimension: BudgetDimension, *, detail: str | None = None) -> None:
        with self._lock:
            self._trip_locked(dimension, detail=detail)

    def reserve_for_call(
        self,
        *,
        estimated_input_tokens: int,
        desired_max_output_tokens: int,
        model: str,
        thinking: bool = False,
        is_subagent: bool = False,
    ) -> BudgetCheckResult:
        """Pre-check all three ceilings, clamp max_tokens, and record in-flight spend."""
        min_output = MIN_OUTPUT_TOKENS_THINKING if thinking else MIN_OUTPUT_TOKENS_DEFAULT
        est_input = max(0, int(estimated_input_tokens))
        clamped_out = max(1, int(desired_max_output_tokens))

        with self._lock:
            if self._exhausted:
                return BudgetCheckResult(
                    allowed=False,
                    exhausted_dimension=self._exhausted_dimension,
                    stop_reason=self._stop_reason,
                    detail=self._exhausted_detail,
                )

            # 1. Wall-clock ceiling.
            timeout_seconds: float | None = None
            if self.limits.max_wall_seconds is not None:
                remaining_wall = self.limits.max_wall_seconds - self.elapsed_seconds()
                if remaining_wall <= MIN_WALL_SECONDS_FOR_CALL:
                    self._trip_locked("wall_seconds")
                    return BudgetCheckResult(
                        allowed=False,
                        exhausted_dimension="wall_seconds",
                        stop_reason=self._stop_reason,
                    )
                timeout_seconds = remaining_wall

            # 2. Total token ceiling + output clamping.
            if self.limits.max_total_tokens is not None:
                avail_tokens = self.limits.max_total_tokens - (
                    self._spent_tokens + self._in_flight_tokens
                )
                max_out_by_tokens = avail_tokens - est_input
                if max_out_by_tokens < min_output:
                    spent_only_out = (
                        self.limits.max_total_tokens - self._spent_tokens - est_input
                    )
                    if self._in_flight_tokens > 0 and spent_only_out >= min_output:
                        return BudgetCheckResult(
                            allowed=False,
                            exhausted_dimension="tokens",
                            contended=True,
                        )
                    self._trip_locked("tokens")
                    return BudgetCheckResult(
                        allowed=False,
                        exhausted_dimension="tokens",
                        stop_reason=self._stop_reason,
                    )
                clamped_out = min(clamped_out, max_out_by_tokens)

            # 3. USD cost ceiling + secondary output clamping.
            price_entry = _lookup_price(self._price_table, model)
            pricing_available = price_entry is not None
            reserved_cost = 0.0
            if self.limits.max_cost_usd is not None:
                if price_entry is None:
                    if model:
                        self._unpriced_models.add(model)
                    if self.limits.unpriced_cost_policy == "fail_closed":
                        self._trip_locked("cost_usd", detail="model_unpriced")
                        return BudgetCheckResult(
                            allowed=False,
                            exhausted_dimension="cost_usd",
                            stop_reason=self._stop_reason,
                            detail="model_unpriced",
                        )
                else:
                    p_in = float(price_entry.get("input", 0.0))
                    p_out = float(price_entry.get("output", 0.0))
                    avail_cost = self.limits.max_cost_usd - (
                        self._spent_cost_usd + self._in_flight_cost_usd
                    )
                    input_cost = (est_input / 1_000_000.0) * p_in

                    def _cost_room(avail: float) -> bool:
                        if avail <= input_cost:
                            return False
                        if p_out <= 0:
                            return True
                        return int(((avail - input_cost) * 1_000_000.0) / p_out) >= min_output

                    if not _cost_room(avail_cost) and self._in_flight_cost_usd > 0 and _cost_room(
                        self.limits.max_cost_usd - self._spent_cost_usd
                    ):
                        return BudgetCheckResult(
                            allowed=False,
                            exhausted_dimension="cost_usd",
                            contended=True,
                        )
                    if avail_cost <= input_cost:
                        self._trip_locked("cost_usd")
                        return BudgetCheckResult(
                            allowed=False,
                            exhausted_dimension="cost_usd",
                            stop_reason=self._stop_reason,
                        )
                    if p_out > 0:
                        max_out_by_cost = int(((avail_cost - input_cost) * 1_000_000.0) / p_out)
                        if max_out_by_cost < min_output:
                            self._trip_locked("cost_usd")
                            return BudgetCheckResult(
                                allowed=False,
                                exhausted_dimension="cost_usd",
                                stop_reason=self._stop_reason,
                            )
                        clamped_out = min(clamped_out, max_out_by_cost)
                    reserved_cost = (est_input * p_in + clamped_out * p_out) / 1_000_000.0
            elif price_entry is not None:
                p_in = float(price_entry.get("input", 0.0))
                p_out = float(price_entry.get("output", 0.0))
                reserved_cost = (est_input * p_in + clamped_out * p_out) / 1_000_000.0

            reserved_tokens = est_input + clamped_out
            reservation = BudgetReservation(
                reservation_id=uuid4().hex,
                model=model,
                estimated_input_tokens=est_input,
                reserved_output_tokens=clamped_out,
                reserved_tokens=reserved_tokens,
                reserved_cost_usd=reserved_cost,
                timeout_seconds=timeout_seconds,
                is_subagent=is_subagent,
                pricing_available=pricing_available,
            )
            self._active_reservations[reservation.reservation_id] = reservation
            self._in_flight_tokens += reserved_tokens
            self._in_flight_cost_usd += reserved_cost
            return BudgetCheckResult(allowed=True, reservation=reservation)

    def release(self, reservation: BudgetReservation | None) -> None:
        """Return an unspent in-flight reservation (e.g. call failed before content)."""
        if reservation is None:
            return
        with self._lock:
            existing = self._active_reservations.pop(reservation.reservation_id, None)
            if existing is None:
                return
            self._in_flight_tokens = max(0, self._in_flight_tokens - existing.reserved_tokens)
            self._in_flight_cost_usd = max(0.0, self._in_flight_cost_usd - existing.reserved_cost_usd)

    def reconcile(
        self,
        reservation: BudgetReservation | None,
        *,
        usage: Mapping[str, Any] | None = None,
        model: str | None = None,
        estimated: bool = False,
    ) -> TaskBudgetSnapshot:
        """Release the reservation, account actual/estimated usage, and check limits."""
        with self._lock:
            is_subagent = False
            resolved_model = model or ""
            if reservation is not None:
                is_subagent = reservation.is_subagent
                resolved_model = resolved_model or reservation.model
                existing = self._active_reservations.pop(reservation.reservation_id, None)
                if existing is not None:
                    self._in_flight_tokens = max(
                        0, self._in_flight_tokens - existing.reserved_tokens
                    )
                    self._in_flight_cost_usd = max(
                        0.0, self._in_flight_cost_usd - existing.reserved_cost_usd
                    )

            if usage is not None:
                normalized = normalize_usage(usage)
                in_tok = normalized["input_tokens"]
                out_tok = normalized["output_tokens"]
                total_tok = normalized["total_tokens"]
                charged = total_tok if total_tok > 0 else (in_tok + out_tok)
                charged = max(0, charged)

                is_estimated = estimated or (
                    USAGE_REPORTED_KEY in usage and not bool(usage.get(USAGE_REPORTED_KEY))
                ) or bool(usage.get("estimated"))

                self._spent_tokens += charged
                self._input_tokens += in_tok
                self._output_tokens += out_tok
                if is_subagent:
                    self._subagent_tokens += charged
                if is_estimated:
                    self._estimated_tokens += charged

                price_entry = _lookup_price(self._price_table, resolved_model)
                if price_entry is not None:
                    cost = _compute_cost_usd(
                        price_entry,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_tokens=normalized["cache_read_tokens"],
                        cache_write_tokens=normalized["cache_write_tokens"],
                    )
                    self._spent_cost_usd += cost
                    self._has_priced_usage = True
                    if is_subagent:
                        self._subagent_cost_usd += cost
                        self._has_subagent_priced_usage = True
                elif resolved_model and self.limits.max_cost_usd is not None:
                    self._unpriced_models.add(resolved_model)

            # Post-reconcile limit checks.
            if (
                self.limits.max_total_tokens is not None
                and self._spent_tokens >= self.limits.max_total_tokens
            ):
                self._trip_locked("tokens")
            elif (
                self.limits.max_wall_seconds is not None
                and self.elapsed_seconds() >= self.limits.max_wall_seconds
            ):
                self._trip_locked("wall_seconds")
            elif (
                self.limits.max_cost_usd is not None
                and self._has_priced_usage
                and self._spent_cost_usd >= self.limits.max_cost_usd
            ):
                self._trip_locked("cost_usd")

            return self._snapshot_locked()

    def _snapshot_locked(self) -> TaskBudgetSnapshot:
        return TaskBudgetSnapshot(
            limits=self.limits.to_dict(),
            spent_tokens=self._spent_tokens,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            subagent_tokens=self._subagent_tokens,
            estimated_tokens=self._estimated_tokens,
            in_flight_tokens=self._in_flight_tokens,
            wall_seconds=self.elapsed_seconds(),
            spent_cost_usd=self._spent_cost_usd if self._has_priced_usage else None,
            subagent_cost_usd=(
                self._subagent_cost_usd if self._has_subagent_priced_usage else None
            ),
            cost_unpriced=bool(self._unpriced_models),
            unpriced_models=tuple(sorted(self._unpriced_models)),
            exhausted=self._exhausted,
            exhausted_dimension=self._exhausted_dimension,
            stop_reason=self._stop_reason,
        )

    def snapshot(self) -> TaskBudgetSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def format_exhausted_notice(self, *, completed_actions: int = 0) -> str:
        snap = self.snapshot()
        dim_labels: Mapping[BudgetDimension, str] = {
            "tokens": "token budget",
            "wall_seconds": "wall-clock time budget",
            "cost_usd": "cost budget",
        }
        dim_name = (
            dim_labels.get(snap.exhausted_dimension, "turn budget")
            if snap.exhausted_dimension is not None
            else "turn budget"
        )
        parts = [f"tokens={snap.spent_tokens}"]
        if self.limits.max_total_tokens is not None:
            parts[-1] += f"/{self.limits.max_total_tokens}"
        parts.append(f"elapsed={snap.wall_seconds:.1f}s")
        if self.limits.max_wall_seconds is not None:
            parts[-1] += f"/{self.limits.max_wall_seconds:g}s"
        if snap.spent_cost_usd is not None:
            cost_part = f"cost=${snap.spent_cost_usd:.4f}"
            if self.limits.max_cost_usd is not None:
                cost_part += f"/${self.limits.max_cost_usd:g}"
            parts.append(cost_part)
        elif self.limits.max_cost_usd is not None and snap.cost_unpriced:
            parts.append(f"cost=unpriced/${self.limits.max_cost_usd:g}")
        if completed_actions > 0:
            parts.append(f"completed_write_actions={completed_actions}")
        return (
            f"Stopped: single-turn {dim_name} reached ({', '.join(parts)}). "
            "No further provider calls were made."
        )


class BatchTokenLedger:
    """Shared in-flight + spent token counter for one sub-agent fan-out batch."""

    BATCH_CONTENDED = 0

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = max(1, int(total_tokens))
        self._lock = threading.RLock()
        self._spent: int = 0
        self._in_flight: int = 0
        self._active: dict[str, int] = {}
        self._exhausted: bool = False

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._exhausted or self._spent >= self.total_tokens

    def available(self) -> int:
        """Headroom left after spent and in-flight tokens (may be negative)."""
        with self._lock:
            return self.total_tokens - (self._spent + self._in_flight)

    def reserve(self, reservation_id: str, est_input: int, desired_out: int, min_out: int) -> int | None:
        """Reserve tokens in the batch.

        Returns the clamped output tokens, ``None`` once the batch is exhausted
        by actual spend, or ``BATCH_CONTENDED`` (0) when only sibling in-flight
        reservations hold the headroom — that case does not exhaust the batch.
        """
        with self._lock:
            if self._exhausted:
                return None
            avail = self.total_tokens - (self._spent + self._in_flight)
            max_out = avail - est_input
            if max_out < min_out:
                if self._in_flight > 0 and self.total_tokens - self._spent - est_input >= min_out:
                    return self.BATCH_CONTENDED
                self._exhausted = True
                return None
            clamped_out = min(desired_out, max_out)
            reserved = est_input + clamped_out
            self._active[reservation_id] = reserved
            self._in_flight += reserved
            return clamped_out

    def release(self, reservation_id: str) -> None:
        with self._lock:
            reserved = self._active.pop(reservation_id, 0)
            if reserved:
                self._in_flight = max(0, self._in_flight - reserved)

    def reconcile(self, reservation_id: str, charged_tokens: int) -> None:
        with self._lock:
            reserved = self._active.pop(reservation_id, 0)
            if reserved:
                self._in_flight = max(0, self._in_flight - reserved)
            self._spent += max(0, int(charged_tokens))
            if self._spent >= self.total_tokens:
                self._exhausted = True


class SubAgentScopedBudget:
    """Three-layer composite budget for one sub-agent: agent cap ∩ batch cap ∩ parent turn."""

    def __init__(
        self,
        *,
        agent_token_budget: int,
        batch_ledger: BatchTokenLedger,
        task_timeout_seconds: float,
        parent: TaskBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent_token_budget = max(1, int(agent_token_budget))
        self.batch_ledger = batch_ledger
        self.task_timeout_seconds = max(0.5, float(task_timeout_seconds))
        self.parent = parent
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.RLock()
        self.cancel_event = asyncio.Event()

        self._agent_spent: int = 0
        self._agent_in_flight: int = 0
        self._active_local: dict[str, int] = {}
        self._exhausted: bool = False
        self._exhausted_dimension: BudgetDimension | None = None
        self._stop_reason: str | None = None
        self._failure_message: str = ""

    @property
    def has_limits(self) -> bool:
        return True

    @property
    def agent_spent_tokens(self) -> int:
        with self._lock:
            return self._agent_spent

    @property
    def exhausted(self) -> bool:
        if self.parent is not None and self.parent.exhausted:
            return True
        if self.batch_ledger.exhausted:
            return True
        with self._lock:
            return self._exhausted or self._agent_spent >= self.agent_token_budget

    @property
    def exhausted_dimension(self) -> BudgetDimension | None:
        if self.parent is not None and self.parent.exhausted:
            return self.parent.exhausted_dimension
        with self._lock:
            if self._exhausted_dimension is not None:
                return self._exhausted_dimension
        if self.batch_ledger.exhausted:
            return "tokens"
        return None

    @property
    def stop_reason(self) -> str | None:
        if self.parent is not None and self.parent.exhausted:
            return self.parent.stop_reason
        with self._lock:
            if self._stop_reason is not None:
                return self._stop_reason
        if self.batch_ledger.exhausted:
            return "budget_tokens_exhausted"
        return None

    @property
    def failure_message(self) -> str:
        if self.parent is not None and self.parent.exhausted:
            return f"parent {self.parent.stop_reason or 'budget_exhausted'}"
        with self._lock:
            if self._failure_message:
                return self._failure_message
        if self.batch_ledger.exhausted:
            return "batch token budget exhausted"
        return ""

    def remaining_wall_seconds(self) -> float | None:
        local_rem = max(0.0, self.task_timeout_seconds - (self._clock() - self._started_at))
        if self.parent is None:
            return local_rem
        parent_rem = self.parent.remaining_wall_seconds()
        if parent_rem is None:
            return local_rem
        return min(local_rem, parent_rem)

    def check_wall_clock(self) -> bool:
        if self.parent is not None and not self.parent.check_wall_clock():
            self.cancel_event.set()
            return False
        rem = self.remaining_wall_seconds()
        if rem is not None and rem <= 0.0:
            with self._lock:
                self._exhausted = True
                self._exhausted_dimension = "wall_seconds"
                self._stop_reason = "budget_wall_time_exhausted"
                self._failure_message = f"timed out after {self.task_timeout_seconds:.0f}s"
            self.cancel_event.set()
            return False
        return True

    def trip(self, dimension: BudgetDimension, *, detail: str | None = None) -> None:
        with self._lock:
            if not self._exhausted:
                self._exhausted = True
                self._exhausted_dimension = dimension
                self._stop_reason = stop_reason_for_dimension(dimension)
                if detail:
                    self._failure_message = detail
        self.cancel_event.set()

    def reserve_for_call(
        self,
        *,
        estimated_input_tokens: int,
        desired_max_output_tokens: int,
        model: str,
        thinking: bool = False,
        is_subagent: bool = True,
    ) -> BudgetCheckResult:
        min_output = MIN_OUTPUT_TOKENS_THINKING if thinking else MIN_OUTPUT_TOKENS_DEFAULT
        est_input = max(0, int(estimated_input_tokens))
        desired_out = max(1, int(desired_max_output_tokens))

        # Narrow the request by this agent's and the batch's own headroom first,
        # so the parent reservation is not inflated by output the local caps
        # would refuse anyway; inflated parent holds starve parallel siblings.
        with self._lock:
            local_out = self.agent_token_budget - (
                self._agent_spent + self._agent_in_flight
            ) - est_input
        batch_out_peek = self.batch_ledger.available() - est_input
        for cap in (local_out, batch_out_peek):
            if cap >= min_output:
                desired_out = min(desired_out, cap)

        # 1. Check parent turn budget first if present.
        parent_res: BudgetReservation | None = None
        if self.parent is not None:
            parent_check = self.parent.reserve_for_call(
                estimated_input_tokens=est_input,
                desired_max_output_tokens=desired_out,
                model=model,
                thinking=thinking,
                is_subagent=True,
            )
            if not parent_check.allowed:
                if not parent_check.contended:
                    self.cancel_event.set()
                return parent_check
            parent_res = parent_check.reservation
            assert parent_res is not None
            desired_out = min(desired_out, parent_res.reserved_output_tokens)

        # 2. Local wall clock.
        local_rem_wall = self.remaining_wall_seconds()
        if local_rem_wall is not None and local_rem_wall <= MIN_WALL_SECONDS_FOR_CALL:
            if parent_res is not None and self.parent is not None:
                self.parent.release(parent_res)
            self.trip("wall_seconds", detail=f"timed out after {self.task_timeout_seconds:.0f}s")
            return BudgetCheckResult(
                allowed=False,
                exhausted_dimension="wall_seconds",
                stop_reason="budget_wall_time_exhausted",
            )

        # 3. Single sub-agent token cap.
        res_id = uuid4().hex
        with self._lock:
            if self._exhausted:
                if parent_res is not None and self.parent is not None:
                    self.parent.release(parent_res)
                return BudgetCheckResult(
                    allowed=False,
                    exhausted_dimension=self._exhausted_dimension,
                    stop_reason=self._stop_reason,
                )
            agent_avail = self.agent_token_budget - (self._agent_spent + self._agent_in_flight)
            max_out_agent = agent_avail - est_input
            if max_out_agent < min_output:
                if parent_res is not None and self.parent is not None:
                    self.parent.release(parent_res)
                self._exhausted = True
                self._exhausted_dimension = "tokens"
                self._stop_reason = "budget_tokens_exhausted"
                self._failure_message = (
                    f"token budget exhausted ({self._agent_spent}/{self.agent_token_budget})"
                )
                self.cancel_event.set()
                return BudgetCheckResult(
                    allowed=False,
                    exhausted_dimension="tokens",
                    stop_reason="budget_tokens_exhausted",
                )
            clamped_out = min(desired_out, max_out_agent)

            # 4. Shared batch token cap (in-flight aware across concurrent siblings).
            batch_out = self.batch_ledger.reserve(res_id, est_input, clamped_out, min_output)
            if batch_out == BatchTokenLedger.BATCH_CONTENDED:
                if parent_res is not None and self.parent is not None:
                    self.parent.release(parent_res)
                return BudgetCheckResult(
                    allowed=False,
                    exhausted_dimension="tokens",
                    contended=True,
                )
            if batch_out is None:
                if parent_res is not None and self.parent is not None:
                    self.parent.release(parent_res)
                self._exhausted = True
                self._exhausted_dimension = "tokens"
                self._stop_reason = "budget_tokens_exhausted"
                self._failure_message = "batch token budget exhausted"
                self.cancel_event.set()
                return BudgetCheckResult(
                    allowed=False,
                    exhausted_dimension="tokens",
                    stop_reason="budget_tokens_exhausted",
                )
            clamped_out = min(clamped_out, batch_out)
            reserved_tokens = est_input + clamped_out
            self._active_local[res_id] = reserved_tokens
            self._agent_in_flight += reserved_tokens

        timeout_seconds = local_rem_wall
        if parent_res is not None and parent_res.timeout_seconds is not None:
            timeout_seconds = (
                min(timeout_seconds, parent_res.timeout_seconds)
                if timeout_seconds is not None
                else parent_res.timeout_seconds
            )

        return BudgetCheckResult(
            allowed=True,
            reservation=BudgetReservation(
                reservation_id=res_id,
                model=model,
                estimated_input_tokens=est_input,
                reserved_output_tokens=clamped_out,
                reserved_tokens=reserved_tokens,
                reserved_cost_usd=parent_res.reserved_cost_usd if parent_res is not None else 0.0,
                timeout_seconds=timeout_seconds,
                is_subagent=True,
                pricing_available=parent_res.pricing_available if parent_res is not None else False,
                parent_reservation=parent_res,
            ),
        )

    def release(self, reservation: BudgetReservation | None) -> None:
        if reservation is None:
            return
        self.batch_ledger.release(reservation.reservation_id)
        with self._lock:
            reserved = self._active_local.pop(reservation.reservation_id, 0)
            if reserved:
                self._agent_in_flight = max(0, self._agent_in_flight - reserved)
        if self.parent is not None and reservation.parent_reservation is not None:
            self.parent.release(reservation.parent_reservation)

    def reconcile(
        self,
        reservation: BudgetReservation | None,
        *,
        usage: Mapping[str, Any] | None = None,
        model: str | None = None,
        estimated: bool = False,
    ) -> TaskBudgetSnapshot | None:
        charged = 0
        if usage is not None:
            normalized = normalize_usage(usage)
            charged = (
                normalized["total_tokens"]
                if normalized["total_tokens"] > 0
                else (normalized["input_tokens"] + normalized["output_tokens"])
            )
            charged = max(0, charged)

        if reservation is not None:
            self.batch_ledger.reconcile(reservation.reservation_id, charged)
            with self._lock:
                reserved = self._active_local.pop(reservation.reservation_id, 0)
                if reserved:
                    self._agent_in_flight = max(0, self._agent_in_flight - reserved)
                self._agent_spent += charged
                if self._agent_spent >= self.agent_token_budget:
                    self._exhausted = True
                    self._exhausted_dimension = "tokens"
                    self._stop_reason = "budget_tokens_exhausted"
                    self._failure_message = (
                        f"token budget exhausted ({self._agent_spent}/{self.agent_token_budget})"
                    )
                    self.cancel_event.set()
                elif self.batch_ledger.exhausted:
                    self._exhausted = True
                    self._exhausted_dimension = "tokens"
                    self._stop_reason = "budget_tokens_exhausted"
                    self._failure_message = "batch token budget exhausted"
                    self.cancel_event.set()

        parent_snapshot: TaskBudgetSnapshot | None = None
        if self.parent is not None:
            parent_snapshot = self.parent.reconcile(
                reservation.parent_reservation if reservation is not None else None,
                usage=usage,
                model=model,
                estimated=estimated,
            )
            if self.parent.exhausted:
                self.cancel_event.set()
        return parent_snapshot

    def snapshot(self) -> TaskBudgetSnapshot:
        if self.parent is not None:
            return self.parent.snapshot()
        return TaskBudgetSnapshot(
            limits={"max_total_tokens": self.agent_token_budget, "max_wall_seconds": self.task_timeout_seconds, "max_cost_usd": None, "unpriced_cost_policy": "warn_only"},
            spent_tokens=self._agent_spent,
            input_tokens=0,
            output_tokens=0,
            subagent_tokens=self._agent_spent,
            estimated_tokens=0,
            in_flight_tokens=self._agent_in_flight,
            wall_seconds=max(0.0, self._clock() - self._started_at),
            spent_cost_usd=None,
            subagent_cost_usd=None,
            cost_unpriced=False,
            unpriced_models=(),
            exhausted=self.exhausted,
            exhausted_dimension=self.exhausted_dimension,
            stop_reason=self.stop_reason,
        )

    def format_exhausted_notice(self, *, completed_actions: int = 0) -> str:
        if self.parent is not None and self.parent.exhausted:
            return self.parent.format_exhausted_notice(completed_actions=completed_actions)
        return self.failure_message or "Sub-agent budget exhausted."


_CURRENT_TASK_BUDGET: ContextVar[TaskBudget | SubAgentScopedBudget | None] = ContextVar(
    "agent_smith_current_task_budget",
    default=None,
)


def current_task_budget() -> TaskBudget | SubAgentScopedBudget | None:
    return _CURRENT_TASK_BUDGET.get()


@contextmanager
def use_task_budget(
    budget: TaskBudget | SubAgentScopedBudget | None,
) -> Iterator[TaskBudget | SubAgentScopedBudget | None]:
    token = _CURRENT_TASK_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_TASK_BUDGET.reset(token)


@contextmanager
def without_task_budget() -> Iterator[None]:
    token = _CURRENT_TASK_BUDGET.set(None)
    try:
        yield
    finally:
        _CURRENT_TASK_BUDGET.reset(token)


CONTENDED_POLL_SECONDS = 0.05
CONTENDED_MAX_POLL_SECONDS = 0.5


async def reserve_for_call_waiting(
    budget: "TaskBudget | SubAgentScopedBudget",
    **kwargs: Any,
) -> BudgetCheckResult:
    """Reserve budget, waiting while sibling in-flight reservations hold the headroom.

    Contention is transient: concurrent sub-agent calls reconcile or release
    their worst-case holds when they finish. Waiting is bounded by the budget's
    wall clock (sub-agents always carry a task timeout) and by cancellation.
    """
    delay = CONTENDED_POLL_SECONDS
    while True:
        check = budget.reserve_for_call(**kwargs)
        if check.allowed or not check.contended:
            return check
        if not budget.check_wall_clock() or budget.exhausted:
            return BudgetCheckResult(
                allowed=False,
                exhausted_dimension=budget.exhausted_dimension,
                stop_reason=budget.stop_reason,
            )
        remaining = budget.remaining_wall_seconds()
        wait = delay if remaining is None else max(0.0, min(delay, remaining))
        try:
            await asyncio.wait_for(budget.cancel_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, CONTENDED_MAX_POLL_SECONDS)


__all__ = (
    "BatchTokenLedger",
    "BudgetCheckResult",
    "BudgetDimension",
    "BudgetReservation",
    "CONTENDED_MAX_POLL_SECONDS",
    "CONTENDED_POLL_SECONDS",
    "MIN_OUTPUT_TOKENS_DEFAULT",
    "MIN_OUTPUT_TOKENS_THINKING",
    "MIN_WALL_SECONDS_FOR_CALL",
    "SubAgentScopedBudget",
    "TaskBudget",
    "TaskBudgetLimits",
    "TaskBudgetSnapshot",
    "UnpricedCostPolicy",
    "current_task_budget",
    "reserve_for_call_waiting",
    "stop_reason_for_dimension",
    "use_task_budget",
    "without_task_budget",
)
