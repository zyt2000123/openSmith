"""Risk tiers that triage tool-approval flows.

``high_risk`` was computed by the guard and carried on ``ApprovalScope`` but
never changed any downstream behavior — every approval-gated call flowed
through the same broker wait.  A tier is derived at the guard, propagated
through the policy decision, the approval request and the emitted events, and
finally *routes* behavior: the registry only caches session whitelists for the
lowest approval tier, so high/critical approvals must be re-granted each time.
"""

from __future__ import annotations

from enum import Enum


class RiskTier(str, Enum):
    """Ordered risk classification for a tool call that reaches approval.

    Order matters: ``max(tier)`` yields the most restrictive tier seen in a
    policy chain.
    """

    ROUTINE = "routine"      # read-only / no side effect; executes without approval
    ELEVATED = "elevated"    # ordinary write or outside-workspace access; ask once
    HIGH = "high"            # sensitive path, network, runtime state; ask, never cache
    CRITICAL = "critical"    # dangerous rule hit or destructive level; ask, never cache

    @property
    def weight(self) -> int:
        return _TIER_WEIGHTS[self]

    @classmethod
    def max(cls, *tiers: "RiskTier | None") -> "RiskTier":
        """Most restrictive non-None tier, defaulting to ROUTINE."""
        return max(
            (tier for tier in tiers if tier is not None),
            key=lambda tier: tier.weight,
            default=RiskTier.ROUTINE,
        )


_TIER_WEIGHTS = {
    RiskTier.ROUTINE: 0,
    RiskTier.ELEVATED: 1,
    RiskTier.HIGH: 2,
    RiskTier.CRITICAL: 3,
}


def risk_for_approval(
    *,
    level: str | None = None,
    high_risk: bool = False,
    network_access: bool = False,
    rule_hit: bool = False,
) -> RiskTier:
    """Derive the risk tier of one approval-gated tool call.

    ``execute`` is deliberately NOT elevated to HIGH here: permission levels
    default to EXECUTE for tools that do not declare one, so the level alone
    cannot distinguish an arbitrary host command from an ordinary read.
    Genuinely high-risk host execution is already caught by the dangerous-rule
    (CRITICAL), sensitive-path (HIGH), and opaque-command scope bindings.
    """
    if rule_hit or level == "destructive":
        return RiskTier.CRITICAL
    if high_risk or network_access:
        return RiskTier.HIGH
    return RiskTier.ELEVATED
