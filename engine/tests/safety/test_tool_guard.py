"""platform-protect-001：pip/uv 安装只在涉及平台路径时拦截，用户项目内放行。"""
import json
import logging
import os
import sys
from pathlib import Path

from engine.safety.fact_gate import FactGate, FactGateContext
from engine.safety.tool_guard import AuditLog, GuardResult, PermissionLevel, ToolGuard
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall, ToolDefinition
from engine.tool.registry import ToolRegistry

_RULES = Path(__file__).resolve().parents[3] / "agents" / "safety" / "dangerous_commands.json"


def _builtin_guard(rules: Path = _RULES, allowed_dirs: list[Path] | None = None) -> ToolGuard:
    registry = ToolRegistry()
    registry.load_providers(Path(__file__).resolve().parents[3] / "agents" / "tools")
    return ToolGuard(rules, allowed_dirs=allowed_dirs, tool_registry=registry.definitions())


def _check(command):
    return _builtin_guard().check(ToolCall(id="t", name="shell", arguments={"command": command}))


def _check_tool(name, arguments):
    return _builtin_guard().check(ToolCall(id="t", name=name, arguments=arguments))


class _FakeGuard:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def check(self, call):
        self.calls.append(call)
        return self.result


def test_tool_policy_allows_without_guard():
    decision = ToolPolicy().evaluate(ToolCall(id="t", name="read_file", arguments={}))

    assert decision.allowed
    assert decision.observation == ""


def test_tool_policy_maps_guard_block_to_observation():
    guard = _FakeGuard(
        GuardResult(
            allowed=False,
            reason="needs approval",
            level=PermissionLevel.DESTRUCTIVE,
            needs_confirmation=True,
        )
    )
    call = ToolCall(id="t", name="shell", arguments={"command": "rm -rf ./x"})

    decision = ToolPolicy(guard).evaluate(call)

    assert guard.calls == [call]
    assert not decision.allowed
    assert decision.reason == "needs approval"
    assert decision.level is PermissionLevel.DESTRUCTIVE
    assert decision.needs_confirmation
    assert decision.observation == "[BLOCKED] needs approval"


def test_pip_install_in_user_project_allowed():
    assert _check("pip install requests").allowed


def test_pip_install_into_platform_blocked():
    assert not _check("pip install --target ~/Downloads/Agent-Smith/engine requests").allowed


def test_pip_install_with_platform_path_before_blocked():
    # 平台路径出现在 pip install 之前也要拦（lookahead 与顺序无关）
    assert not _check("PIP_TARGET=~/Downloads/Agent-Smith/vendor pip install requests").allowed


def test_uv_add_in_user_project_allowed():
    assert _check("uv add httpx").allowed


def test_rm_platform_data_blocked():
    assert not _check("rm -rf ~/.agent-smith/agent").allowed


def test_memory_views_may_be_written_by_the_memory_path():
    memory = Path.home() / ".agent-smith" / "agent" / "memory"
    assert _check(
        "printf '%s\\n' event >> ~/.agent-smith/agent/memory/recent.jsonl"
    ).allowed
    assert _check(
        f"printf '%s\\n' event >> {memory / 'recent.jsonl'}"
    ).allowed
    assert _check(
        f"printf '%s\\n' view > {memory / 'recent.md'}"
    ).allowed
    assert _check(
        f"printf '%s\\n' facts > {memory / 'durable.md'}"
    ).allowed


def test_platform_writes_outside_memory_remain_blocked():
    agent_dir = Path.home() / ".agent-smith" / "agent"
    memory = agent_dir / "memory"
    assert not _check(
        f"printf '%s\\n' token > {agent_dir / 'config.yaml'}"
    ).allowed
    assert not _check(
        f"printf '%s\\n' payload > {memory / 'unknown.txt'}"
    ).allowed
    assert not _check(
        f"pip install --target {memory} requests"
    ).allowed


def test_combined_redirect_to_platform_data_blocked():
    """``&>`` and ``&>>`` (stdout+stderr) redirects into platform data must be blocked."""
    agent_dir = Path.home() / ".agent-smith" / "agent"
    assert not _check(f"cmd &> {agent_dir / 'evil.log'}").allowed
    assert not _check(f"cmd &>> {agent_dir / 'evil.log'}").allowed


def test_extract_shell_paths_captures_combined_redirect():
    from engine.safety.tool_guard import _extract_shell_write_paths

    write_paths = _extract_shell_write_paths("cmd &> ~/.agent-smith/agent/x")
    assert any(p.endswith("x") for p in write_paths)
    write_paths = _extract_shell_write_paths("cmd &>> ~/.agent-smith/agent/x")
    assert any(p.endswith("x") for p in write_paths)


def test_file_tools_only_write_approved_memory_views_in_platform_data():
    agent_dir = Path.home() / ".agent-smith" / "agent"
    memory = agent_dir / "memory"
    assert _check_tool(
        "write_file", {"path": str(memory / "recent.jsonl"), "content": "event"}
    ).allowed
    assert _check_tool(
        "edit_file", {"path": str(memory / "durable.md"), "old_string": "a", "new_string": "b"}
    ).allowed
    assert not _check_tool(
        "write_file", {"path": str(agent_dir / "config.yaml"), "content": "nope"}
    ).allowed
    assert not _check_tool(
        "edit_file", {"path": str(memory / "unknown.txt"), "old_string": "a", "new_string": "b"}
    ).allowed


def test_memory_exception_does_not_bypass_fact_gate():
    memory_file = Path.home() / ".agent-smith" / "agent" / "memory" / "recent.jsonl"
    call = ToolCall(
        id="t",
        name="shell",
        arguments={"command": f"printf '%s\\n' event >> {memory_file}"},
    )
    gate = FactGate(FactGateContext("session", "turn"))
    policy = ToolPolicy(_builtin_guard(), fact_gate=gate)

    first = policy.evaluate(call)
    assert not first.allowed
    assert first.challenged

    policy.begin_round()
    second = policy.evaluate(call)
    assert not second.allowed
    assert second.approval_required
    assert second.needs_confirmation


def test_path_tools_are_guarded():
    blocked_calls = [
        ("grep", {"pattern": "root", "path": "/etc"}),
        ("glob_files", {"pattern": "*.conf", "path": "/etc"}),
        ("list_dir", {"path": "/etc"}),
        ("edit_file", {"path": "/etc/hosts", "old_string": "a", "new_string": "b"}),
        ("git_ops", {"action": "worktree_remove", "path": "/etc"}),
        ("git_ops", {"action": "commit", "cwd": str(Path.cwd()), "files": ["/etc/passwd"]}),
        ("shell", {"command": "pwd", "cwd": "/etc"}),
    ]
    for name, arguments in blocked_calls:
        assert not _check_tool(name, arguments).allowed, name


def test_registry_normalizes_legacy_alias_before_metadata_policy_check():
    registry = ToolRegistry()
    registry.register("web_search", "", {}, lambda: "OK", permission_level="read")
    registry.register("web_fetch", "", {}, lambda: "OK", permission_level="read")
    guard = ToolGuard(_RULES, tool_registry=registry.definitions())

    search = registry.normalize_call(ToolCall(id="search", name="websearch", arguments={"query": "docs"}))
    fetch = registry.normalize_call(ToolCall(id="fetch", name="webfetch", arguments={"url": "https://example.com"}))

    assert search.name == "web_search"
    assert fetch.name == "web_fetch"
    assert guard.check(search).level is PermissionLevel.READ
    assert guard.check(fetch).level is PermissionLevel.READ


def test_metadata_declared_path_args_are_guarded_without_hardcoded_entry():
    # custom_writer is absent from ToolGuard's fallback tables — checks must
    # come purely from the declared ToolDefinition metadata.
    defn = ToolDefinition(
        name="custom_writer",
        description="",
        path_args=("target",),
        is_write_tool=True,
    )
    guard = ToolGuard(_RULES, tool_registry={"custom_writer": defn})

    outside = guard.check(
        ToolCall(id="t", name="custom_writer", arguments={"target": "/etc/hosts"})
    )
    assert not outside.allowed

    env_write = guard.check(
        ToolCall(
            id="t",
            name="custom_writer",
            arguments={"target": str(Path.home() / "proj" / ".env")},
        )
    )
    assert not env_write.allowed
    assert env_write.needs_confirmation


def test_scalar_path_arg_rejects_a_non_string_value():
    """A list/dict supplied for a scalar path argument must be rejected, not
    str()-stringified into a bogus path: the guard would otherwise check a
    different path than the provider interprets, creating a check/execute
    divergence."""
    defn = ToolDefinition(
        name="custom_reader",
        description="",
        path_args=("path",),
        is_write_tool=False,
    )
    guard = ToolGuard(_RULES, tool_registry={"custom_reader": defn})

    result = guard.check(
        ToolCall(id="t", name="custom_reader", arguments={"path": ["/etc/passwd", "/etc/shadow"]})
    )

    assert not result.allowed
    assert "must be a string" in result.reason


def test_metadata_permission_level_controls_registered_tool():
    defn = ToolDefinition(name="notes_read", description="", permission_level="read")
    guard = ToolGuard(_RULES, tool_registry={"notes_read": defn})

    assert guard.check(ToolCall(id="t", name="notes_read", arguments={})).level is PermissionLevel.READ
    # Without metadata an unknown tool stays at the EXECUTE default.
    assert ToolGuard(_RULES).check(ToolCall(id="t", name="notes_read", arguments={})).level is PermissionLevel.EXECUTE


def test_unregistered_tool_is_held_for_approval_instead_of_name_based_fallback():
    result = ToolGuard(_RULES).check(ToolCall(id="t", name="legacy_unknown", arguments={}))

    assert result.allowed
    assert result.level is PermissionLevel.EXECUTE
    assert result.approval_required is True


def test_metadata_read_actions_do_not_require_approval_but_writes_still_do():
    defn = ToolDefinition(
        name="memory_ops",
        description="",
        permission_level="write",
        approval_policy="policy",
        side_effect="write",
        read_actions=frozenset({"search"}),
    )
    guard = ToolGuard(_RULES, tool_registry={"memory_ops": defn})

    searched = guard.check(ToolCall(id="read", name="memory_ops", arguments={"action": "search"}))
    wrote = guard.check(ToolCall(id="write", name="memory_ops", arguments={"action": "remember"}))

    assert searched.approval_required is False
    assert wrote.approval_required is True


def test_session_whitelist_extends_boundary_but_not_sensitive_blocks():
    guard = _builtin_guard()
    call = ToolCall(id="t", name="list_dir", arguments={"path": "/opt/data/project"})

    assert not guard.check(call).allowed

    guard.whitelist.allow_path("/opt/data")
    assert guard.check(call).allowed

    # Sensitive paths stay blocked even when whitelisted.
    ssh_path = str(Path.home() / ".ssh")
    guard.whitelist.allow_path(ssh_path)
    assert not guard.check(ToolCall(id="t", name="list_dir", arguments={"path": ssh_path})).allowed


def test_session_tool_whitelist_does_not_bypass_sensitive_paths(tmp_path: Path):
    guard = _builtin_guard(allowed_dirs=[tmp_path])
    guard.whitelist.allow_tool("write_file")

    result = guard.check(
        ToolCall(
            id="t",
            name="write_file",
            arguments={"path": str(tmp_path / ".env"), "content": "secret"},
        )
    )

    assert not result.allowed
    assert result.needs_confirmation


def test_project_instruction_whitelist_allows_only_smith_md(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[])
    smith_file = project_root / ".smith" / "SMITH.md"

    assert not guard.check(
        ToolCall(id="t", name="write_file", arguments={"path": str(smith_file), "content": "rules"})
    ).allowed

    assert guard.allow_project_instruction_path(project_root) == smith_file
    result = guard.check(
        ToolCall(id="t", name="write_file", arguments={"path": str(smith_file), "content": "rules"})
    )
    assert result.allowed
    assert result.approval_required

    assert not guard.check(
        ToolCall(id="t", name="write_file", arguments={"path": str(project_root / "README.md"), "content": "no"})
    ).allowed
    assert not guard.check(
        ToolCall(
            id="t",
            name="write_file",
            arguments={"path": str(smith_file / "escaped.md"), "content": "no"},
        )
    ).allowed


def test_write_tool_requests_approval_after_hard_guard_passes(tmp_path: Path):
    definition = ToolDefinition(
        name="write_file",
        description="",
        path_args=("path",),
        is_write_tool=True,
        permission_level="write",
        approval_policy="policy",
        side_effect="write",
    )
    guard = ToolGuard(
        tmp_path / "missing-rules.json",
        allowed_dirs=[tmp_path],
        tool_registry={"write_file": definition},
    )

    result = guard.check(
        ToolCall(
            id="t",
            name="write_file",
            arguments={"path": str(tmp_path / "notes.txt"), "content": "safe"},
        )
    )

    assert result.allowed
    assert result.approval_required
    assert result.level is PermissionLevel.WRITE


def test_working_directory_restricts_relative_and_absolute_tool_paths(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside = tmp_path / "outside.txt"
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)

    relative = guard.check(
        ToolCall(id="relative", name="write_file", arguments={"path": "notes.md"})
    )
    absolute = guard.check(
        ToolCall(id="absolute", name="write_file", arguments={"path": str(outside)})
    )

    assert relative.allowed
    assert relative.approval_required
    assert not absolute.allowed
    assert absolute.boundary_block
    assert absolute.approval_required
    assert absolute.needs_confirmation


def test_runtime_provider_files_are_non_delegable_write_targets(tmp_path: Path):
    project_dir = tmp_path / "project"
    provider_dir = project_dir / "agents" / "tools"
    provider_dir.mkdir(parents=True)
    provider_file = provider_dir / "todo.py"
    provider_file.write_text("original\n", encoding="utf-8")
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)
    guard.set_non_delegable_write_roots([provider_dir])

    result = guard.check(
        ToolCall(
            id="provider-write",
            name="write_file",
            arguments={"path": str(provider_file), "content": "changed"},
        )
    )

    assert not result.allowed
    assert not result.approval_required
    assert "runtime-provider-001" in result.reason


def test_runtime_provider_hardlink_alias_is_non_delegable_write_target(tmp_path: Path):
    project_dir = tmp_path / "project"
    provider_dir = project_dir / "agents" / "tools"
    provider_dir.mkdir(parents=True)
    provider_file = provider_dir / "todo.py"
    provider_file.write_text("original\n", encoding="utf-8")
    alias = project_dir / "safe-looking.py"
    os.link(provider_file, alias)
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)
    guard.set_non_delegable_write_roots([provider_dir])

    result = guard.check(
        ToolCall(
            id="provider-alias-write",
            name="write_file",
            arguments={"path": str(alias), "content": "changed"},
        )
    )

    assert not result.allowed
    assert not result.approval_required
    assert "unsafe-alias-001" in result.reason


def test_working_directory_turns_user_credential_paths_into_high_risk_approval(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)

    result = guard.check(
        ToolCall(
            id="credential-path",
            name="list_dir",
            arguments={"path": str(tmp_path / ".ssh")},
        )
    )

    assert not result.allowed
    assert result.approval_required
    assert not result.boundary_block
    assert result.approval_scope is not None
    assert result.approval_scope.high_risk
    assert result.approval_scope.target == str(tmp_path / ".ssh")


def test_scoped_guard_rejects_unnormalized_optional_directory_paths(tmp_path: Path):
    definition = ToolDefinition(
        name="list_dir",
        description="",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
        path_args=("path",),
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    guard = ToolGuard(
        tmp_path / "missing-rules.json", tool_registry={"list_dir": definition}
    )
    guard.set_working_directory(project_dir)

    missing = guard.check(ToolCall(id="missing", name="list_dir", arguments={}))
    empty = guard.check(ToolCall(id="empty", name="list_dir", arguments={"path": ""}))

    assert not missing.allowed
    assert not empty.allowed
    assert "canonical" in missing.reason
    assert "canonical" in empty.reason


def test_audit_log_recursively_redacts_sensitive_argument_values(tmp_path: Path):
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.record(
        "web_fetch",
        {
            "authorization": "Bearer top-level-secret",
            "request": {
                "client_secret": "nested-secret",
                "headers": [{"X-Api-Key": "list-secret"}],
                "label": "safe value",
            },
            "ordinary": "still visible",
        },
        GuardResult(allowed=True),
        metadata={"refreshToken": "extra-secret"},
    )

    entry = json.loads(log_path.read_text(encoding="utf-8"))

    assert entry["args_summary"]["authorization"] == "***"
    assert entry["args_summary"]["request"]["client_secret"] == "***"
    assert entry["args_summary"]["request"]["headers"][0]["X-Api-Key"] == "***"
    assert entry["args_summary"]["request"]["label"] == "safe value"
    assert entry["metadata"]["refreshToken"] == "***"


def test_audit_log_reports_append_failure_without_blocking_execution(
    caplog,
    monkeypatch,
    tmp_path: Path,
):
    audit = AuditLog(tmp_path / "audit.jsonl")

    def fail_open(*_args, **_kwargs):
        raise OSError("audit disk unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    with caplog.at_level(logging.WARNING, logger="engine.safety.tool_guard"):
        audit.record(
            "shell",
            {"command": "pwd"},
            GuardResult(allowed=True),
            call_id="call-1",
            run_id="run-1",
        )

    assert "failed to append tool safety audit" in caplog.text


def test_working_directory_allows_sandboxed_shell_to_reach_approval(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)

    result = guard.check(
        ToolCall(
            id="sandboxed",
            name="shell",
            arguments={"command": "pwd", "cwd": str(project_dir)},
        )
    )

    assert result.allowed
    assert result.approval_required
    assert result.level is PermissionLevel.EXECUTE


def test_working_directory_disables_unconfined_host_shell_execution(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "nested").mkdir()
    definition = ToolDefinition(
        name="shell",
        description="",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
        path_args=("cwd",),
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        side_effect="external",
        execution_environment="host",
    )
    guard = ToolGuard(
        tmp_path / "missing-rules.json",
        tool_registry={"shell": definition},
    )
    guard.set_working_directory(project_dir)

    attempts = [
        guard.check(ToolCall(id="safe", name="shell", arguments={"command": "pwd"})),
        guard.check(ToolCall(id="cd", name="shell", arguments={"command": "cd /tmp && pwd"})),
        guard.check(ToolCall(id="traversal", name="shell", arguments={"command": "cat ../secret.txt"})),
        guard.check(ToolCall(id="absolute", name="shell", arguments={"command": "cat /tmp/secret.txt"})),
        guard.check(ToolCall(id="substitution", name="shell", arguments={"command": 'cd $(dirname "$PWD") && pwd'})),
    ]

    assert all(not result.allowed for result in attempts)
    assert all("requires a sandbox" in result.reason for result in attempts)


def test_sensitive_user_write_becomes_high_risk_approval(tmp_path: Path):
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])

    result = guard.check(
        ToolCall(
            id="t",
            name="write_file",
            arguments={"path": str(tmp_path / ".env"), "content": "secret"},
        )
    )

    assert not result.allowed
    assert result.needs_confirmation
    assert result.approval_required
    assert result.approval_scope is not None
    assert result.approval_scope.high_risk


def test_dangerous_command_becomes_high_risk_approval_not_a_policy_denial():
    result = _check("rm -rf ./generated")

    assert not result.allowed
    assert result.approval_required
    assert result.needs_confirmation
    assert result.level is PermissionLevel.DESTRUCTIVE
    assert result.approval_scope is not None
    assert result.approval_scope.kind == "host_command"
    assert result.approval_scope.high_risk


def test_sensitive_system_file_is_classified_before_generic_path_boundary():
    result = _check_tool("read_file", {"path": "/etc/shadow"})

    assert not result.allowed
    assert result.approval_required
    assert result.approval_scope is not None
    assert result.approval_scope.high_risk
    assert "sens-file-001" in result.reason


def test_runtime_provider_configuration_is_non_delegable_even_after_user_approval(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)

    result = guard.check(
        ToolCall(
            id="runtime-config",
            name="read_file",
            arguments={"path": str(Path.home() / ".agent-smith" / "config.yaml")},
        )
    )

    assert not result.allowed
    assert not result.approval_required
    assert "runtime credential" in result.reason.lower()


def test_runtime_provider_configuration_hardlink_alias_is_not_approvable(
    monkeypatch,
    tmp_path: Path,
):
    import engine.safety.tool_guard as tool_guard_module

    runtime_config = tmp_path / "agent-runtime" / "config.yaml"
    runtime_config.parent.mkdir()
    runtime_config.write_text("api_key: must-not-delegate\n", encoding="utf-8")
    alias = tmp_path / "apparently-user-owned.txt"
    os.link(runtime_config, alias)
    monkeypatch.setattr(
        tool_guard_module,
        "_RUNTIME_CREDENTIAL_PATHS",
        frozenset({runtime_config.resolve()}),
    )
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])

    result = guard.check(
        ToolCall(id="runtime-alias", name="read_file", arguments={"path": str(alias)})
    )

    assert not result.allowed
    assert not result.approval_required
    assert "hard-link alias" in result.reason


def test_network_tools_request_an_exact_user_approval():
    result = _check_tool("web_fetch", {"url": "https://example.com/report"})

    assert result.allowed
    assert result.approval_required
    assert result.approval_scope is not None
    assert result.approval_scope.kind == "network"
    assert result.approval_scope.target == "https://example.com/report"


def test_dollar_anchored_rule_patterns_match_raw_argument_values():
    home = Path.home()

    pem = _check_tool("read_file", {"path": str(home / "certs" / "server.pem")})
    assert not pem.allowed
    assert "sens-file-004" in pem.reason

    env = _check_tool("read_file", {"path": str(home / "proj" / ".env")})
    assert not env.allowed
    assert "sens-file-003" in env.reason

    # Exclude patterns still apply — .env.example stays readable.
    assert _check_tool("read_file", {"path": str(home / "proj" / ".env.example")}).allowed


# ── S1: case-variant / unlisted sensitive-file reads must require approval ──


def test_case_variant_sensitive_file_reads_require_high_risk_approval(tmp_path: Path):
    """`.ENV` and `key.PEM` bypass the case-sensitive regex rules on APFS."""
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])
    for name in (".ENV", "key.PEM"):
        path = tmp_path / name
        path.write_text("SECRET=1\n", encoding="utf-8")
        result = guard.check(
            ToolCall(id="t", name="read_file", arguments={"path": str(path)})
        )
        assert not result.allowed, name
        assert result.approval_required, name
        assert result.needs_confirmation, name
        assert result.approval_scope is not None and result.approval_scope.high_risk, name


def test_env_variant_and_stray_private_key_reads_require_high_risk_approval(
    tmp_path: Path,
):
    """`.env.staging`, `.env.dev`, `id_rsa` match no regex rule but hold secrets."""
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])
    for name in (".env.staging", ".env.dev", "id_rsa", "id_ed25519", "id_rsa_old"):
        path = tmp_path / name
        path.write_text("x\n", encoding="utf-8")
        result = guard.check(
            ToolCall(id="t", name="read_file", arguments={"path": str(path)})
        )
        assert not result.allowed, name
        assert result.approval_scope is not None and result.approval_scope.high_risk, name


def test_documented_env_templates_remain_readable(tmp_path: Path):
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[tmp_path])
    for name in (".env.example", ".env.template", ".env.sample"):
        path = tmp_path / name
        path.write_text("KEY=value\n", encoding="utf-8")
        result = guard.check(
            ToolCall(id="t", name="read_file", arguments={"path": str(path)})
        )
        assert result.allowed, name


# ── S2a: .git credential-bearing reads must require approval ──


def test_read_git_config_and_credentials_require_high_risk_approval(tmp_path: Path):
    git_dir = tmp_path / "proj" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://user:token@example.com/repo\n',
        encoding="utf-8",
    )
    (git_dir / "credentials").write_text(
        "https://user:token@example.com\n", encoding="utf-8"
    )
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[git_dir.parent])

    for name in ("config", "credentials"):
        result = guard.check(
            ToolCall(id="t", name="read_file", arguments={"path": str(git_dir / name)})
        )
        assert not result.allowed, name
        assert result.approval_required, name
        assert result.approval_scope is not None and result.approval_scope.high_risk, name


def test_other_git_metadata_reads_stay_ordinary(tmp_path: Path):
    git_dir = tmp_path / "proj" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    guard = _builtin_guard(tmp_path / "missing-rules.json", allowed_dirs=[git_dir.parent])

    result = guard.check(
        ToolCall(id="t", name="read_file", arguments={"path": str(git_dir / "HEAD")})
    )
    assert result.allowed


# ── S3: cp/install/dd into platform data must escalate to high-risk ──


def test_cp_install_dd_into_platform_data_require_high_risk_approval():
    for command in (
        "cp notes.txt ~/.agent-smith/agent/notes",
        "install notes.txt ~/.agent-smith/agent/notes",
        "dd if=notes.txt of=~/.agent-smith/agent/notes",
        "cp notes.txt ~/.AGENT-SMITH/agent/notes",
    ):
        result = _check(command)
        assert not result.allowed, command
        assert result.approval_required, command
        assert result.approval_scope is not None and result.approval_scope.high_risk, command


def test_cp_into_user_project_stays_allowed():
    assert _check("cp notes.txt ~/Downloads/project/notes").allowed


# ── S4: the audit log keeps a single append handle ──


def test_audit_log_reuses_one_handle_and_reopens_on_path_change(tmp_path: Path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    audit = AuditLog(first)

    audit.record("shell", {"command": "pwd"}, GuardResult(allowed=True), call_id="c1")
    handle = audit._handle
    assert handle is not None

    audit.record("shell", {"command": "ls"}, GuardResult(allowed=True), call_id="c2")
    assert audit._handle is handle

    audit._path = second
    audit.record("shell", {"command": "cat"}, GuardResult(allowed=True), call_id="c3")

    assert len(first.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert len(second.read_text(encoding="utf-8").strip().splitlines()) == 1
    audit.close()


def test_audit_log_redacts_secret_flag_pairs_in_list_arguments(tmp_path: Path):
    log_path = tmp_path / "audit.jsonl"
    audit = AuditLog(log_path)
    audit.record(
        "shell",
        {"command": "run", "args": ["--token", "sk-secret-123456", "--model", "gpt-4o"]},
        GuardResult(allowed=True),
        call_id="c1",
    )
    audit.close()

    entry = json.loads(log_path.read_text(encoding="utf-8"))
    assert entry["args_summary"]["args"] == ["--token", "***", "--model", "gpt-4o"]


# ── S6: sensitive-key redaction comes from a single shared source ──


def test_sensitive_key_redaction_comes_from_the_shared_approval_source():
    import engine.safety.approval as approval_module
    import engine.safety.tool_guard as tool_guard_module

    # tool_guard must import the helper rather than carry a duplicate tuple.
    assert not hasattr(tool_guard_module, "_SENSITIVE_ARG_KEY_PARTS")
    assert (
        tool_guard_module._is_sensitive_argument_name
        is approval_module._is_sensitive_argument_name
    )


def test_guard_and_approval_redact_the_same_argument_keys():
    from engine.safety.approval import summarize_arguments
    from engine.safety.tool_guard import _summarize_args

    arguments = {"dbPassword": "secret", "auth_token": "t", "normal": "plain"}
    assert _summarize_args(arguments)["dbPassword"] == "***"
    assert _summarize_args(arguments)["auth_token"] == "***"
    assert summarize_arguments(arguments)["dbPassword"] == "***"
    assert summarize_arguments(arguments)["auth_token"] == "***"
    assert _summarize_args(arguments)["normal"] == "plain"
    assert summarize_arguments(arguments)["normal"] == "plain"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)


# ── Review: the guard must still see the path the caller actually wrote ──


def _workspace_guard(workspace: Path) -> tuple[ToolGuard, ToolRegistry]:
    """Build the guard exactly as ``prepare_runtime`` does for one request."""
    provider_dir = Path(__file__).resolve().parents[3] / "agents" / "tools"
    registry = ToolRegistry()
    registry.load_builtin_providers(provider_dir)
    registry.bind_working_directory(workspace)
    guard = ToolGuard(_RULES)
    guard.set_working_directory(workspace)
    guard.set_non_delegable_write_roots([provider_dir])
    guard.bind_definitions(registry.definitions())
    return guard, registry


def _check_declared(guard: ToolGuard, registry: ToolRegistry, tool: str, path: str):
    call = registry.normalize_call(
        ToolCall(id="t", name=tool, arguments={"path": path, "content": "x"})
    )
    return guard.check(call, audit=False)


def test_symlinked_git_dir_still_requires_high_risk_write_approval(tmp_path: Path):
    """normalize_call resolves paths before the guard runs, which erased the
    ``.git`` component a symlinked .git resolves away from — demoting a git-hook
    write (code that runs on the next git operation) to a routine write."""
    workspace = (tmp_path / "ws").resolve()
    store = workspace / "gitstore"
    (store / "hooks").mkdir(parents=True)
    (store / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    os.symlink(store, workspace / ".git")

    guard, registry = _workspace_guard(workspace)
    result = _check_declared(guard, registry, "write_file", ".git/hooks/pre-commit")

    assert not result.allowed
    assert result.approval_required
    assert result.risk.value == "high"
    assert ".git" in result.reason


def test_symlinked_git_dir_still_gates_credential_bearing_config_read(tmp_path: Path):
    """.git/config embeds ``https://user:token@host`` remote URLs, and read_file
    is approval_policy="never" — so losing the .git classification made this a
    free credential read with no user interaction at all."""
    workspace = (tmp_path / "ws").resolve()
    store = workspace / "gitstore"
    store.mkdir(parents=True)
    (store / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (store / "config").write_text("[remote]\n\turl = https://u:tok@h/x\n", encoding="utf-8")
    os.symlink(store, workspace / ".git")

    guard, registry = _workspace_guard(workspace)
    call = registry.normalize_call(
        ToolCall(id="t", name="read_file", arguments={"path": ".git/config"})
    )
    result = guard.check(call, audit=False)

    assert not result.allowed
    assert result.approval_required
    assert result.risk.value == "high"


def test_symlinked_credential_dir_still_requires_high_risk_approval(tmp_path: Path):
    workspace = (tmp_path / "ws").resolve()
    store = workspace / "keystore"
    store.mkdir(parents=True)
    os.symlink(store, workspace / ".ssh")

    guard, registry = _workspace_guard(workspace)
    result = _check_declared(guard, registry, "write_file", ".ssh/authorized_keys")

    assert not result.allowed
    assert result.risk.value == "high"
    assert ".ssh" in result.reason


def test_declared_path_survives_the_registry_execution_recheck(tmp_path: Path):
    """``ToolRegistry.execute`` re-normalizes as a backstop; re-deriving the
    declared view there would collapse it onto the resolved path and make the
    second guard check weaker than the first."""
    workspace = (tmp_path / "ws").resolve()
    store = workspace / "gitstore"
    (store / "hooks").mkdir(parents=True)
    (store / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    os.symlink(store, workspace / ".git")

    _guard, registry = _workspace_guard(workspace)
    once = registry.normalize_call(
        ToolCall(id="t", name="write_file", arguments={"path": ".git/hooks/pre-commit"})
    )
    twice = registry.normalize_call(once)

    assert once.declared_paths == twice.declared_paths
    assert once.declared_paths["path"].endswith("/.git/hooks/pre-commit")
    assert once.arguments["path"] == twice.arguments["path"]


def test_empty_allowed_dirs_is_deny_all_not_the_permissive_default(tmp_path: Path):
    """``if allowed_dirs:`` conflated None (unconfigured) with [] (deny-all), so
    a caller asking for a deny-all baseline silently got [home, /tmp, cwd] and
    its one-file whitelist became a no-op."""
    empty = ToolGuard(_RULES, allowed_dirs=[])
    default = ToolGuard(_RULES, allowed_dirs=None)

    assert empty.file_guard._allowed == []
    assert default.file_guard._allowed != []
    outside = tmp_path / "anywhere" / "victim.txt"
    assert not empty.file_guard.check_path(str(outside), writing=True).allowed


def test_concurrent_audit_logs_keep_one_verifiable_chain(tmp_path: Path):
    """AuditLog is built per request but appends to one install-wide log.  With
    per-instance chain state, two concurrent runs assigned the same seq from the
    same prev_hash and verify() reported tampering on an untampered log."""
    log_path = tmp_path / "audit.jsonl"
    run_a = AuditLog(log_path)
    run_b = AuditLog(log_path)

    run_a.record("read_file", {"path": "/a/1"}, GuardResult(allowed=True), run_id="run-A")
    run_b.record("read_file", {"path": "/b/1"}, GuardResult(allowed=True), run_id="run-B")
    run_a.record("read_file", {"path": "/a/2"}, GuardResult(allowed=True), run_id="run-A")
    run_b.record("read_file", {"path": "/b/2"}, GuardResult(allowed=True), run_id="run-B")

    verification = run_a.verify()
    assert verification.ok, verification.failure
    assert verification.records == 4
    sequences = [
        json.loads(line)["seq"]
        for line in log_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert sequences == [1, 2, 3, 4]
    run_a.close()
