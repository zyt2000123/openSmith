"""Declarative safety rules (``safety.rules``) and repeat sensitive call guard.

Configuration lives under the top-level ``safety:`` section of the agent's
``config.yaml`` and fails closed on any malformed rule, regex, action, risk,
or overly broad egress allowlist entry.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from engine.safety.risk import RiskTier
from engine.safety.taint import EgressAllowlist, EgressTarget
from engine.tool.interface import ToolCall, ToolDefinition

TaintEgressAction = Literal["deny", "ask", "audit"]
RepeatWindow = Literal["run"]
RepeatTriggerAction = Literal["confirm", "deny"]
RuleAction = Literal["deny", "ask", "allow"]

_VALID_TAINT_ACTIONS = frozenset({"deny", "ask", "audit"})
# Only per-run windowing is implemented; reject unimplemented values fail-closed (L1).
_VALID_REPEAT_WINDOWS = frozenset({"run"})
_VALID_REPEAT_TRIGGERS = frozenset({"confirm", "deny"})
_VALID_RULE_ACTIONS = frozenset({"deny", "ask", "allow"})
_VALID_RISK_MAP: dict[str, RiskTier] = {
    "routine": RiskTier.ROUTINE,
    "elevated": RiskTier.ELEVATED,
    "high": RiskTier.HIGH,
    "critical": RiskTier.CRITICAL,
}


@dataclass(frozen=True)
class SafetyTaintConfig:
    enabled: bool = True
    on_egress: TaintEgressAction = "deny"
    allowlist: EgressAllowlist = field(default_factory=EgressAllowlist)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> SafetyTaintConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("safety.taint must be a mapping")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("safety.taint.enabled must be a boolean")
        on_egress = raw.get("on_egress", "deny")
        if not isinstance(on_egress, str) or on_egress not in _VALID_TAINT_ACTIONS:
            raise ValueError(
                f"safety.taint.on_egress must be one of {sorted(_VALID_TAINT_ACTIONS)}, got {on_egress!r}"
            )
        raw_allow = raw.get("egress_allowlist", ())
        if raw_allow is None:
            raw_allow = ()
        if not isinstance(raw_allow, (list, tuple)):
            raise ValueError("safety.taint.egress_allowlist must be a list of strings")
        allowlist = EgressAllowlist(raw_allow)
        return cls(
            enabled=enabled,
            on_egress=on_egress,  # type: ignore[arg-type]
            allowlist=allowlist,
        )


@dataclass(frozen=True)
class RepeatGuardConfig:
    enabled: bool = True
    window: RepeatWindow = "run"
    threshold: int = 2
    min_risk: RiskTier = RiskTier.HIGH
    on_trigger: RepeatTriggerAction = "confirm"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> RepeatGuardConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("safety.repeat_guard must be a mapping")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("safety.repeat_guard.enabled must be a boolean")
        window = raw.get("window", "run")
        if not isinstance(window, str) or window not in _VALID_REPEAT_WINDOWS:
            raise ValueError(
                f"safety.repeat_guard.window must be one of {sorted(_VALID_REPEAT_WINDOWS)}, got {window!r}"
            )
        threshold = raw.get("threshold", 2)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 2:
            raise ValueError("safety.repeat_guard.threshold must be an integer >= 2")
        raw_risk = raw.get("min_risk", "high")
        if not isinstance(raw_risk, str) or raw_risk.lower() not in _VALID_RISK_MAP:
            raise ValueError(
                f"safety.repeat_guard.min_risk must be one of {sorted(_VALID_RISK_MAP)}, got {raw_risk!r}"
            )
        on_trigger = raw.get("on_trigger", "confirm")
        if not isinstance(on_trigger, str) or on_trigger not in _VALID_REPEAT_TRIGGERS:
            raise ValueError(
                f"safety.repeat_guard.on_trigger must be one of {sorted(_VALID_REPEAT_TRIGGERS)}, got {on_trigger!r}"
            )
        return cls(
            enabled=enabled,
            window=window,  # type: ignore[arg-type]
            threshold=threshold,
            min_risk=_VALID_RISK_MAP[raw_risk.lower()],
            on_trigger=on_trigger,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class RuleCondition:
    arg_equals: tuple[tuple[str, Any], ...] = ()
    arg_regex: tuple[tuple[str, re.Pattern[str]], ...] = ()
    tainted: bool | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None, *, rule_id: str) -> RuleCondition:
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError(f"safety.rules[{rule_id}].when must be a mapping")
        allowed_keys = {"arg_equals", "arg_regex", "tainted"}
        unknown = set(raw.keys()) - allowed_keys
        if unknown:
            raise ValueError(
                f"safety.rules[{rule_id}].when has unknown keys: {', '.join(sorted(unknown))}"
            )

        eq_pairs: list[tuple[str, Any]] = []
        raw_eq = raw.get("arg_equals")
        if raw_eq is not None:
            if not isinstance(raw_eq, Mapping):
                raise ValueError(f"safety.rules[{rule_id}].when.arg_equals must be a mapping")
            for k, v in raw_eq.items():
                if not isinstance(k, str) or not k:
                    raise ValueError(f"safety.rules[{rule_id}].when.arg_equals keys must be strings")
                eq_pairs.append((k, v))

        rx_pairs: list[tuple[str, re.Pattern[str]]] = []
        raw_rx = raw.get("arg_regex")
        if raw_rx is not None:
            if not isinstance(raw_rx, Mapping):
                raise ValueError(f"safety.rules[{rule_id}].when.arg_regex must be a mapping")
            for k, v in raw_rx.items():
                if not isinstance(k, str) or not k or not isinstance(v, str):
                    raise ValueError(
                        f"safety.rules[{rule_id}].when.arg_regex entries must be string->regex"
                    )
                try:
                    compiled = re.compile(v)
                except re.error as exc:
                    raise ValueError(
                        f"safety.rules[{rule_id}].when.arg_regex.{k} invalid regex {v!r}: {exc}"
                    ) from exc
                rx_pairs.append((k, compiled))

        tainted = raw.get("tainted")
        if tainted is not None and not isinstance(tainted, bool):
            raise ValueError(f"safety.rules[{rule_id}].when.tainted must be a boolean")

        return cls(
            arg_equals=tuple(eq_pairs),
            arg_regex=tuple(rx_pairs),
            tainted=tainted,
        )

    def matches(self, arguments: Mapping[str, Any], *, tainted: bool) -> bool:
        if self.tainted is not None and self.tainted != tainted:
            return False
        for key, expected in self.arg_equals:
            if key not in arguments or arguments[key] != expected:
                return False
        for key, pattern in self.arg_regex:
            value = arguments.get(key)
            if not isinstance(value, str) or pattern.search(value) is None:
                return False
        return True


@dataclass(frozen=True)
class DeclarativeRule:
    id: str
    priority: int
    action: RuleAction
    tools: tuple[str, ...]
    when: RuleCondition = field(default_factory=RuleCondition)
    risk: RiskTier = RiskTier.HIGH
    reason: str = ""
    override_builtin_approval: bool = False

    def matches(self, call: ToolCall, *, tainted: bool) -> bool:
        if self.tools and not any(
            fnmatch.fnmatchcase(call.name, pat) for pat in self.tools
        ):
            return False
        return self.when.matches(call.arguments, tainted=tainted)


@dataclass(frozen=True)
class SafetyConfig:
    taint: SafetyTaintConfig = field(default_factory=SafetyTaintConfig)
    repeat_guard: RepeatGuardConfig = field(default_factory=RepeatGuardConfig)
    rules: tuple[DeclarativeRule, ...] = ()

    @classmethod
    def from_profile_config(cls, profile_config: Mapping[str, Any] | None) -> SafetyConfig:
        if not isinstance(profile_config, Mapping):
            return cls()
        raw_safety = profile_config.get("safety")
        if raw_safety is None:
            return cls()
        if not isinstance(raw_safety, Mapping):
            raise ValueError("safety configuration must be a mapping")

        taint = SafetyTaintConfig.from_mapping(raw_safety.get("taint"))
        repeat_guard = RepeatGuardConfig.from_mapping(raw_safety.get("repeat_guard"))

        raw_rules = raw_safety.get("rules", [])
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, (list, tuple)):
            raise ValueError("safety.rules must be a list of rule objects")

        parsed_rules: list[tuple[int, int, DeclarativeRule]] = []
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw_rules):
            if not isinstance(item, Mapping):
                raise ValueError(f"safety.rules[{idx}] must be a mapping")
            rule_id = item.get("id")
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError(f"safety.rules[{idx}].id must be a non-empty string")
            rule_id = rule_id.strip()
            if rule_id in seen_ids:
                raise ValueError(f"Duplicate safety rule id: {rule_id!r}")
            seen_ids.add(rule_id)

            priority = item.get("priority", 0)
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ValueError(f"safety.rules[{rule_id}].priority must be an integer")

            action = item.get("action")
            if not isinstance(action, str) or action not in _VALID_RULE_ACTIONS:
                raise ValueError(
                    f"safety.rules[{rule_id}].action must be one of {sorted(_VALID_RULE_ACTIONS)}, got {action!r}"
                )

            raw_tools = item.get("tools", ())
            if not isinstance(raw_tools, list) or not raw_tools or not all(
                isinstance(t, str) and t.strip() for t in raw_tools
            ):
                raise ValueError(
                    f"safety.rules[{rule_id}].tools must be a non-empty list of tool patterns"
                )

            default_risk = "critical" if action == "deny" else "high"
            raw_risk = item.get("risk", default_risk)
            if not isinstance(raw_risk, str) or raw_risk.lower() not in _VALID_RISK_MAP:
                raise ValueError(
                    f"safety.rules[{rule_id}].risk must be one of {sorted(_VALID_RISK_MAP)}, got {raw_risk!r}"
                )

            override_builtin = item.get("override_builtin_approval", False)
            if not isinstance(override_builtin, bool):
                raise ValueError(
                    f"safety.rules[{rule_id}].override_builtin_approval must be a boolean"
                )

            reason = item.get("reason", "")
            if not isinstance(reason, str):
                raise ValueError(f"safety.rules[{rule_id}].reason must be a string")

            when = RuleCondition.from_mapping(item.get("when"), rule_id=rule_id)
            rule = DeclarativeRule(
                id=rule_id,
                priority=priority,
                action=action,  # type: ignore[arg-type]
                tools=tuple(t.strip() for t in raw_tools),
                when=when,
                risk=_VALID_RISK_MAP[raw_risk.lower()],
                reason=reason.strip() or f"Matched safety rule '{rule_id}'",
                override_builtin_approval=override_builtin,
            )
            parsed_rules.append((-priority, idx, rule))

        parsed_rules.sort(key=lambda t: (t[0], t[1]))
        return cls(
            taint=taint,
            repeat_guard=repeat_guard,
            rules=tuple(rule for _, _, rule in parsed_rules),
        )

    def match_deny(self, call: ToolCall, *, tainted: bool) -> DeclarativeRule | None:
        for rule in self.rules:
            if rule.action == "deny" and rule.matches(call, tainted=tainted):
                return rule
        return None

    def match_non_deny(self, call: ToolCall, *, tainted: bool) -> DeclarativeRule | None:
        for rule in self.rules:
            if rule.action != "deny" and rule.matches(call, tainted=tainted):
                return rule
        return None


BucketKey = tuple[str, str]


def classify_sensitive_bucket(
    call: ToolCall,
    definition: ToolDefinition | None,
    *,
    is_sensitive_path: bool = False,
    is_outside_write: bool = False,
    is_host_or_dangerous: bool = False,
    egress_target: EgressTarget | None = None,
    tainted: bool = False,
    risk: RiskTier = RiskTier.ROUTINE,
    min_risk: RiskTier = RiskTier.HIGH,
) -> tuple[BucketKey, str] | None:
    """Classify a tool call into a semantic repeat-guard bucket, or ``None``."""
    is_write = bool(definition is not None and definition.is_write_tool)
    if is_sensitive_path:
        op = "write" if is_write else "read"
        return (("sensitive_path", op), f"sensitive_path:{op}")
    if is_host_or_dangerous:
        return (("host_command", call.name), f"host_command:{call.name}")
    if is_outside_write:
        return (("outside_workspace_write", call.name), f"outside_workspace_write:{call.name}")
    if egress_target is not None and (tainted or risk >= min_risk):
        return (("egress", egress_target.kind), f"egress:{egress_target.kind}")
    if risk >= min_risk:
        return (("high_risk", call.name), f"high_risk:{call.name}")
    return None


class RepeatSensitiveChecker:
    """Counts sensitive tool executions per semantic bucket within one run/session."""

    def __init__(self, config: RepeatGuardConfig | None = None) -> None:
        self.config = config if config is not None else RepeatGuardConfig()
        self._counts: dict[BucketKey, int] = {}

    def next_count(self, bucket: BucketKey) -> int:
        return self._counts.get(bucket, 0) + 1

    def should_trigger(self, bucket: BucketKey) -> tuple[bool, int]:
        count = self.next_count(bucket)
        if not self.config.enabled:
            return False, count
        return count >= self.config.threshold, count

    def record(self, bucket: BucketKey) -> int:
        updated = self._counts.get(bucket, 0) + 1
        self._counts[bucket] = updated
        return updated

    def reset(self) -> None:
        self._counts.clear()


__all__ = (
    "BucketKey",
    "DeclarativeRule",
    "RepeatGuardConfig",
    "RepeatSensitiveChecker",
    "RuleAction",
    "RuleCondition",
    "SafetyConfig",
    "SafetyTaintConfig",
    "classify_sensitive_bucket",
)
