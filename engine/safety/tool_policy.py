"""Unified policy layer for deciding whether tool calls may execute."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from engine.safety.approval import ApprovalScope
from engine.safety.fact_gate import FactGate
from engine.safety.tool_guard import PermissionLevel
from engine.tool.interface import ToolCall

_LEVEL_ORDER = {
    PermissionLevel.READ: 0,
    PermissionLevel.WRITE: 1,
    PermissionLevel.EXECUTE: 2,
    PermissionLevel.DESTRUCTIVE: 3,
}

if TYPE_CHECKING:
    from engine.safety.tool_guard import ToolGuard


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str = ""
    level: PermissionLevel = PermissionLevel.READ
    needs_confirmation: bool = False
    approval_required: bool = False
    challenged: bool = False
    approval_scope: ApprovalScope | None = None

    @property
    def observation(self) -> str:
        if self.allowed:
            return ""
        if self.challenged:
            return f"[PREFLIGHT] {self.reason}"
        return f"[BLOCKED] {self.reason}"


@runtime_checkable
class PolicyChecker(Protocol):
    """Protocol for composable policy checkers.

    Implementors return a ``ToolPolicyDecision`` that either blocks
    (``allowed=False``) or passes (``allowed=True``).  The first blocker
    in the chain wins.
    """

    def check_policy(self, call: ToolCall) -> ToolPolicyDecision: ...


class _ToolGuardAdapter:
    """Adapt :class:`ToolGuard` to the :class:`PolicyChecker` protocol."""

    def __init__(self, guard: "ToolGuard") -> None:
        self._guard = guard

    def check_policy(self, call: ToolCall) -> ToolPolicyDecision:
        result = self._guard.check(call)
        if result.approval_required:
            return ToolPolicyDecision(
                allowed=False,
                reason=result.reason,
                level=result.level,
                needs_confirmation=True,
                approval_required=True,
                approval_scope=result.approval_scope,
            )
        return ToolPolicyDecision(
            allowed=result.allowed,
            reason=result.reason,
            level=result.level,
            needs_confirmation=result.needs_confirmation,
            approval_scope=result.approval_scope,
        )


class _FactGateAdapter:
    """Adapt :class:`FactGate` to the :class:`PolicyChecker` protocol."""

    def __init__(self, gate: FactGate) -> None:
        self._gate = gate

    def check_policy(self, call: ToolCall) -> ToolPolicyDecision:
        result = self._gate.evaluate(call)
        if result.challenged:
            return ToolPolicyDecision(
                allowed=False,
                reason=result.reason,
                challenged=True,
            )
        return ToolPolicyDecision(allowed=True)

    def begin_round(self) -> None:
        self._gate.begin_round()


class ToolPolicy:
    """Single policy gateway used before any runtime tool execution.

    Accepts an ordered list of :class:`PolicyChecker` instances.  The
    convenience parameters *guard* and *fact_gate* are kept for backward
    compatibility and are converted into checkers internally.
    """

    def __init__(
        self,
        guard: "ToolGuard | None" = None,
        *,
        fact_gate: FactGate | None = None,
        checkers: list[PolicyChecker] | None = None,
    ) -> None:
        self._checkers: list[PolicyChecker] = []
        if guard is not None:
            self._checkers.append(_ToolGuardAdapter(guard))
        if fact_gate is not None:
            self._checkers.append(_FactGateAdapter(fact_gate))
        if checkers:
            self._checkers.extend(checkers)

    def evaluate(self, call: ToolCall) -> ToolPolicyDecision:
        level = PermissionLevel.READ
        deferred_approval: ToolPolicyDecision | None = None
        for checker in self._checkers:
            decision = checker.check_policy(call)
            if not decision.allowed:
                # Fact-forcing challenges deliberately take precedence over a
                # later user approval.  The first attempt must establish the
                # requested facts; only the retry may pause for approval.
                if decision.approval_required:
                    deferred_approval = ToolPolicyDecision(
                        allowed=False,
                        reason=decision.reason,
                        level=decision.level if decision.level != PermissionLevel.READ else level,
                        needs_confirmation=decision.needs_confirmation,
                        approval_required=True,
                        challenged=decision.challenged,
                        approval_scope=decision.approval_scope,
                    )
                    continue
                return ToolPolicyDecision(
                    allowed=False,
                    reason=decision.reason,
                    level=decision.level if decision.level != PermissionLevel.READ else level,
                    needs_confirmation=decision.needs_confirmation,
                    approval_required=decision.approval_required,
                    challenged=decision.challenged,
                    approval_scope=decision.approval_scope,
                )
            # Carry forward the most specific permission level seen so far.
            if _LEVEL_ORDER.get(decision.level, 0) > _LEVEL_ORDER.get(level, 0):
                level = decision.level
        if deferred_approval is not None:
            return deferred_approval
        return ToolPolicyDecision(allowed=True, level=level)

    def begin_round(self) -> None:
        for checker in self._checkers:
            if hasattr(checker, "begin_round"):
                checker.begin_round()
