"""platform-protect-001：pip/uv 安装只在涉及平台路径时拦截，用户项目内放行。"""
import json
import sys
from pathlib import Path

from engine.safety.fact_gate import FactGate, FactGateContext
from engine.safety.tool_guard import AuditLog, GuardResult, PermissionLevel, ToolGuard
from engine.safety.tool_policy import ToolPolicy
from engine.tool.interface import ToolCall, ToolDefinition
from engine.tool.registry import ToolRegistry

_RULES = Path(__file__).resolve().parents[2] / "agents" / "safety" / "dangerous_commands.json"


def _builtin_guard(rules: Path = _RULES, allowed_dirs: list[Path] | None = None) -> ToolGuard:
    registry = ToolRegistry()
    registry.load_providers(Path(__file__).resolve().parents[2] / "agents" / "tools")
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
    from engine.safety.tool_guard import _extract_shell_paths

    _, write_paths = _extract_shell_paths("cmd &> ~/.agent-smith/agent/x")
    assert any(p.endswith("x") for p in write_paths)
    _, write_paths = _extract_shell_paths("cmd &>> ~/.agent-smith/agent/x")
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


def test_working_directory_disables_unconfined_shell_execution(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "nested").mkdir()
    guard = _builtin_guard(tmp_path / "missing-rules.json")
    guard.set_working_directory(project_dir)

    attempts = [
        guard.check(ToolCall(id="safe", name="shell", arguments={"command": "pwd"})),
        guard.check(ToolCall(id="cd", name="shell", arguments={"command": "cd /tmp && pwd"})),
        guard.check(ToolCall(id="traversal", name="shell", arguments={"command": "cat ../secret.txt"})),
        guard.check(ToolCall(id="absolute", name="shell", arguments={"command": "cat /tmp/secret.txt"})),
        guard.check(ToolCall(id="substitution", name="shell", arguments={"command": 'cd $(dirname "$PWD") && pwd'})),
    ]

    assert all(not result.allowed for result in attempts)
    assert all("unavailable" in result.reason for result in attempts)


def test_sensitive_write_remains_hard_blocked_and_not_approvable(tmp_path: Path):
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
    assert not result.approval_required


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
