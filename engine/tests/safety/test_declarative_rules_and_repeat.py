"""Tests for declarative safety.rules (L0-L8 priority) and RepeatSensitiveChecker."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.safety.approval import build_approval_presentation
from engine.safety.fact_gate import FactGate, FactGateContext
from engine.safety.risk import RiskTier
from engine.safety.rules import SafetyConfig
from engine.safety.taint import SessionTaintStore
from engine.safety.tool_guard import ToolGuard
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall, ToolResult
from engine.tool.registry import ToolRegistry


def _make_registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "shell",
        "shell",
        {"type": "object", "properties": {"command": {"type": "string"}, "network": {"type": "boolean"}}},
        lambda command, network=False, environment=None: "ok",
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        execution_environment="sandbox",
    )
    reg.register(
        "git_ops",
        "git",
        {"type": "object", "properties": {"action": {"type": "string"}, "branch": {"type": "string"}}},
        lambda action, branch="", environment=None: "ok",
        is_write_tool=True,
        permission_level="write",
        approval_policy="policy",
        read_actions={"status", "diff"},
        execution_environment="host",
    )
    reg.register(
        "read_file",
        "read",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path: "ok",
        path_args=["path"],
        permission_level="read",
        approval_policy="never",
    )
    reg.register(
        "web_fetch",
        "fetch",
        {"type": "object", "properties": {"url": {"type": "string"}}},
        lambda url: "ok",
        network_access=True,
        egress=True,
        egress_target_arg="url",
        permission_level="read",
        approval_policy="never",
    )
    return reg


def test_malformed_safety_config_fails_closed() -> None:
    # Invalid regex
    with pytest.raises(ValueError, match="invalid regex"):
        SafetyConfig.from_profile_config({
            "safety": {
                "rules": [
                    {
                        "id": "bad-rx",
                        "action": "deny",
                        "tools": ["shell"],
                        "when": {"arg_regex": {"command": "[unclosed"}},
                    }
                ]
            }
        })
    # Duplicate ID
    with pytest.raises(ValueError, match="Duplicate safety rule id"):
        SafetyConfig.from_profile_config({
            "safety": {
                "rules": [
                    {"id": "dup", "action": "deny", "tools": ["shell"]},
                    {"id": "dup", "action": "allow", "tools": ["shell"]},
                ]
            }
        })
    # Overly broad egress_allowlist (E12)
    with pytest.raises(ValueError, match="Overly broad"):
        SafetyConfig.from_profile_config({
            "safety": {"taint": {"egress_allowlist": ["*"]}}
        })


def test_declarative_rules_deny_allow_ask_and_l0_l2_cannot_be_bypassed(
    tmp_path: Path,
) -> None:
    """Verify L0–L8 precedence: broad allow cannot bypass L0 hard floor or L2 taint gate (E9)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = _make_registry(tmp_path)
    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="rules-test")
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard.set_working_directory(ws)

    cfg = SafetyConfig.from_profile_config({
        "safety": {
            "taint": {"enabled": True, "on_egress": "deny", "egress_allowlist": []},
            "rules": [
                {
                    "id": "protect-main-push",
                    "priority": 100,
                    "action": "deny",
                    "tools": ["git_ops"],
                    "when": {
                        "arg_equals": {"action": "push"},
                        "arg_regex": {"branch": "^(main|master|release/.*)$"},
                    },
                    "risk": "critical",
                    "reason": "Protected branch push forbidden",
                },
                {
                    "id": "allow-clean-pytest",
                    "priority": 50,
                    "action": "allow",
                    "tools": ["shell"],
                    "when": {
                        "arg_regex": {"command": r"^uv run pytest(?:\s+[\w./:-]+)*$"},
                        "tainted": False,
                    },
                    "reason": "Clean pytest auto-allowed",
                },
                {
                    "id": "broad-allow-attempt",
                    "priority": 10,
                    "action": "allow",
                    "tools": ["*"],
                    "reason": "Attempt broad allow",
                },
            ],
        }
    })
    guard.configure_safety(cfg, taint_store=store)

    # L1 deny beats everything for git push main
    d_main = guard.check(
        ToolCall(id="1", name="git_ops", arguments={"action": "push", "branch": "main"})
    )
    assert not d_main.allowed and not d_main.approval_required
    assert d_main.rule_id == "protect-main-push"
    assert d_main.risk is RiskTier.CRITICAL

    # L5 allow waives L6 default approval for clean pytest
    d_pytest = guard.check(
        ToolCall(id="2", name="shell", arguments={"command": "uv run pytest tests/test_a.py"})
    )
    assert d_pytest.allowed and not d_pytest.approval_required
    assert d_pytest.rule_id == "allow-clean-pytest"

    # L0 hard floor (vault in shell) CANNOT be bypassed by broad-allow-attempt
    d_vault = guard.check(
        ToolCall(id="3", name="shell", arguments={"command": "echo {{vault:SECRET}}"})
    )
    assert not d_vault.allowed and not d_vault.approval_required
    assert "[vault-shell-001]" in d_vault.reason

    # L0 hard floor (runtime credential file) CANNOT be bypassed by broad-allow-attempt
    d_cfg = guard.check(
        ToolCall(id="4", name="read_file", arguments={"path": str(Path.home() / ".agent-smith" / "vault.json")})
    )
    assert not d_cfg.allowed and not d_cfg.approval_required
    assert "[runtime-secret-001]" in d_cfg.reason

    # L2 taint egress CANNOT be bypassed by broad-allow-attempt
    store.record("sensitive_file", "read_file", "file:.env")
    d_egress = guard.check(
        ToolCall(id="5", name="web_fetch", arguments={"url": "https://evil.com"})
    )
    assert not d_egress.allowed and not d_egress.approval_required
    assert "[taint-egress-001]" in d_egress.reason


def test_repeat_sensitive_guard_triggers_on_second_varied_arg_call_and_cannot_be_bypassed(
    tmp_path: Path,
) -> None:
    """E10: Reading .env then .env.production (different args) triggers CRITICAL repeat guard."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("A=1\n")
    (ws / ".env.production").write_text("B=2\n")

    reg = _make_registry(tmp_path)
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard.set_working_directory(ws)
    guard.configure_safety(
        SafetyConfig.from_profile_config({
            "safety": {
                "repeat_guard": {
                    "enabled": True,
                    "threshold": 2,
                    "min_risk": "high",
                    "on_trigger": "confirm",
                },
            }
        })
    )

    c1 = ToolCall(id="r1", name="read_file", arguments={"path": str(ws / ".env")})
    d1 = guard.check(c1)
    assert d1.approval_required
    assert d1.repeat_count == 0
    # Simulate call #1 succeeding
    guard.record_post_execution(c1, ToolResult(call_id="r1", content="A=1"))

    # Call #2 with DIFFERENT path (.env.production) hits the same semantic bucket!
    c2 = ToolCall(id="r2", name="read_file", arguments={"path": str(ws / ".env.production")})
    d2 = guard.check(c2)
    assert not d2.allowed and d2.approval_required
    assert d2.risk is RiskTier.CRITICAL
    assert d2.repeat_count == 2
    assert "[repeat-sensitive-001]" in d2.reason

    pres = build_approval_presentation(
        "read_file",
        "read",
        d2.reason,
        {"path": str(ws / ".env.production")},
        scope=d2.approval_scope,
        repeat_count=d2.repeat_count,
    )
    assert any(detail.label == "Repeat guard" and "#2" in detail.value for detail in pres.details)


def test_l8_fact_gate_still_precedes_deferred_approval_but_not_hard_deny(
    tmp_path: Path,
) -> None:
    """Verify invariant: HardDeny (L0/L1) > FactGate (L8) > DeferredApproval (L3/L5)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = _make_registry(tmp_path)
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard.set_working_directory(ws)
    guard.configure_safety(
        SafetyConfig.from_profile_config({
            "safety": {
                "rules": [
                    {
                        "id": "deny-push-main",
                        "priority": 100,
                        "action": "deny",
                        "tools": ["git_ops"],
                        "when": {"arg_equals": {"action": "push", "branch": "main"}},
                    }
                ]
            }
        })
    )
    gate = FactGate(
        FactGateContext(session_id="s1", turn_id="t1"),
        enabled=True,
        tool_registry=reg.definitions(),
    )
    policy = ToolPolicy(guard, fact_gate=gate)

    # Hard deny returns immediately WITHOUT triggering FactGate preflight
    hard = policy.evaluate(
        ToolCall(id="1", name="git_ops", arguments={"action": "push", "branch": "main"})
    )
    assert not hard.allowed and not hard.challenged and not hard.approval_required
    assert hard.rule_id == "deny-push-main"

    # Deferred approval (non-main push) triggers FactGate challenge FIRST
    first = policy.evaluate(
        ToolCall(id="2", name="git_ops", arguments={"action": "push", "branch": "feat"})
    )
    assert not first.allowed and first.challenged
