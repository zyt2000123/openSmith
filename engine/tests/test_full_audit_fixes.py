"""Regression tests for the full-module audit in engine/AUDIT-2026-07-28.md.

Distinct from tests/safety/test_audit_review_fixes.py, which covers the earlier
same-day review (audit-record dedup, symlinks, prune markers, ledger idempotency).
One test per fixed defect here, each written to fail against the pre-fix code.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(message: str):
    """Route a message through the public, cross-identity seam.

    Deliberately not poking at a single IdentitySpec: routes are split across
    several shipped identity files, and which one holds a given route is not this
    test's concern — ``resolve()`` is what production actually calls.
    """
    from engine.identity.catalog import load_identity_catalog

    catalog = load_identity_catalog(_REPO_ROOT / "agents" / "identities", force=True)
    return catalog.resolve(message)


# ── P0-1: case-insensitive filesystems bypass the .git/.ssh write guard ──

def test_sensitive_dir_guard_is_case_insensitive(tmp_path: Path) -> None:
    """`.GIT/hooks/pre-commit` hits the real `.git` on APFS — it must be blocked."""
    from engine.safety.tool_guard import FileGuard

    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    for variant in (".git", ".GIT", ".Git", ".gIt"):
        result = guard.check_path(f"{variant}/hooks/pre-commit", writing=True)
        assert not result.allowed, f"write into {variant}/ must be blocked"


def test_always_blocked_dir_guard_is_case_insensitive(tmp_path: Path) -> None:
    """Credential directories must be blocked regardless of path casing."""
    from engine.safety.tool_guard import FileGuard

    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    for variant in (".ssh", ".SSH", ".Ssh", ".gnupg", ".GnuPG", ".AWS", ".Kube"):
        result = guard.check_path(f"{variant}/id_rsa", writing=True)
        assert not result.allowed, f"access to {variant}/ must be blocked"


def test_hardlink_to_protected_file_is_not_writable(tmp_path: Path) -> None:
    """A harmless-looking name that shares an inode with a git hook must be blocked.

    Found by the round-1 review of the casing fix: every other check reasons about
    path names, so a hard link writes straight through to the real hook.
    """
    import os

    from engine.safety.tool_guard import FileGuard

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\noriginal\n")
    link = tmp_path / "harmless.sh"
    os.link(hook, link)

    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    result = guard.check_path("harmless.sh", writing=True)
    assert not result.allowed, "writing a hard link to a protected file must be blocked"


def test_single_linked_files_are_still_writable(tmp_path: Path) -> None:
    """The hard-link check must not block ordinary single-linked writes."""
    from engine.safety.tool_guard import FileGuard

    plain = tmp_path / "src" / "main.py"
    plain.parent.mkdir(parents=True)
    plain.write_text("print('hi')\n")

    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    assert guard.check_path("src/main.py", writing=True).allowed
    assert guard.check_path("src/brand_new.py", writing=True).allowed


def test_hardlinks_unrelated_to_protected_files_stay_writable(tmp_path: Path) -> None:
    """Round-2 review: refusing every multiply-linked file breaks pnpm/ccache trees.

    Only a link that actually shares an inode with a protected file may be
    refused — st_nlink > 1 on its own is normal in content-addressed stores.
    """
    import os

    from engine.safety.tool_guard import FileGuard

    (tmp_path / ".git").mkdir()
    shared = tmp_path / "store" / "asset.txt"
    shared.parent.mkdir(parents=True)
    shared.write_text("shared content\n")
    linked = tmp_path / "pkg" / "asset.txt"
    linked.parent.mkdir(parents=True)
    os.link(shared, linked)

    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    assert os.lstat(linked).st_nlink == 2, "precondition: the file is multiply linked"
    result = guard.check_path("pkg/asset.txt", writing=True)
    assert result.allowed, f"unrelated hard link must stay writable: {result.reason}"


def test_ordinary_dotted_directories_are_still_writable(tmp_path: Path) -> None:
    """Case folding must not over-block unrelated dot directories."""
    from engine.safety.tool_guard import FileGuard

    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    guard = FileGuard()
    guard.set_working_directory(tmp_path)

    result = guard.check_path(".github/workflows/ci.yml", writing=True)
    assert result.allowed, f"unexpected block: {result.reason}"


# ── P1-1: substring keyword matching misroutes English requests ──

def test_latin_keywords_require_word_boundaries() -> None:
    """'digital' must not match the 'git' keyword and steal the coding pipeline."""
    decision = _resolve(
        "Please add a digital signature verification feature to the login flow"
    )
    assert decision.route_id == "feature", f"expected feature, got {decision.route_id}"
    assert decision.pipeline_id == "coding"

    decision = _resolve("Refactor the legitimate config loader")
    assert decision.route_id == "refactor", f"expected refactor, got {decision.route_id}"
    assert decision.pipeline_id == "coding"


def test_git_route_still_matches_real_git_requests() -> None:
    """The boundary fix must not break genuine git routing, English or Chinese."""
    for message in ("git status please", "rebase onto main", "把这个分支合并一下"):
        decision = _resolve(message)
        assert decision.route_id == "git", f"{message!r} should route to git"


def test_english_inflections_still_match_keywords() -> None:
    """A trailing \\b would kill every plural/gerund — only the leading one is asserted.

    ``\\bcommit\\b`` does not match "squash these commits" because the plural "s"
    is a word character, so asserting both boundaries silently breaks ordinary
    English phrasing across every route.
    """
    expected = {
        "squash these commits": "git",
        "delete stale branches": "git",
        "already merged that upstream": "git",
        "fix the bugs in the parser": "bugfix",
        "debugging the crash report": "bugfix",
        "adding a new endpoint": "feature",
        "refactoring the loader": "refactor",
    }
    for message, route_id in expected.items():
        decision = _resolve(message)
        assert decision.route_id == route_id, (
            f"{message!r} routed to {decision.route_id}, expected {route_id}"
        )


# ── P1-3: CJK sentences never match the trigram FTS index ──

@pytest.mark.asyncio
async def test_cjk_sentence_query_finds_episode(tmp_path: Path) -> None:
    """A whole-sentence Chinese query must retrieve a relevant episode."""
    from engine.memory.search import SearchIndex

    idx = SearchIndex(tmp_path)
    await idx.open()
    try:
        await idx.index_entry(
            "ep1", "用户上周要求优化数据库查询性能，已通过索引调整解决该问题。", "episode"
        )
        hits = await idx.search("之前数据库查询性能的问题怎么解决的", top_k=5)
        assert hits, "whole-sentence CJK query must not return empty"
        assert hits[0]["id"] == "ep1"
    finally:
        await idx.close()


@pytest.mark.asyncio
async def test_short_cjk_query_still_works(tmp_path: Path) -> None:
    """The pre-existing short-keyword path must keep working."""
    from engine.memory.search import SearchIndex

    idx = SearchIndex(tmp_path)
    await idx.open()
    try:
        await idx.index_entry("ep1", "关于记忆系统的讨论", "episode")
        hits = await idx.search("记忆", top_k=5)
        assert hits and hits[0]["id"] == "ep1"
    finally:
        await idx.close()


def test_stem_plus_consonant_plus_e_does_not_misroute() -> None:
    """Round-3 review: an unrestricted ``[a-z]?`` reopened the prefix collision.

    It absorbed any single letter before the ``e`` suffix, so bug→"bugle",
    add→"addle", design→"designee" matched again.  The doubled letter is now
    restricted to a repeat of the stem's own last character.
    """
    for message in (
        "the trumpeter played a bugle solo",
        "stress can addle your thinking",
        "she is the designee for this contract renewal",
    ):
        decision = _resolve(message)
        assert decision.route_id == "direct", (
            f"{message!r} misrouted to {decision.route_id}"
        )


def test_adverb_inflection_still_matches() -> None:
    """Round-3 review: tightening the suffix set dropped "wrongly"."""
    decision = _resolve("the config behaves wrongly")
    assert decision.route_id == "bugfix", f"got {decision.route_id}"


def test_hardlink_check_covers_git_worktree_layout(tmp_path: Path) -> None:
    """Round-3 review: in a worktree ``.git`` is a file, so the scan was skipped.

    The real hooks live in the main repository, and this project itself uses a
    parallel-worktree workflow — so the guard was inert exactly where it mattered.
    """
    import os

    from engine.safety.tool_guard import FileGuard

    main_repo = tmp_path / "main"
    hooks = main_repo / ".git" / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\noriginal\n")
    # Mirror what git actually writes, including the marker files the pointer
    # validation looks for.
    (main_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    gitdir = main_repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/wt\n")
    (gitdir / "commondir").write_text("../..\n")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    link = worktree / "innocuous_backup.txt"
    os.link(hook, link)

    guard = FileGuard()
    guard.set_working_directory(worktree)

    result = guard.check_path("innocuous_backup.txt", writing=True)
    assert not result.allowed, "hard link into a worktree's real hooks must be blocked"


def test_long_cjk_run_does_not_crowd_out_later_terms() -> None:
    """Round-3 review: a long leading CJK run filled every _MAX_TERMS slot.

    The old whole-sentence term could never hit the cap, so slicing introduced
    this truncation risk along with the fix.
    """
    from engine.memory.search import _MAX_TERMS, _search_terms

    terms = _search_terms(
        "今天我们讨论了一下关于项目进度和预算超支的问题以及后续计划安排 urgent"
    )
    assert len(terms) <= _MAX_TERMS
    assert "urgent" in terms, f"trailing keyword was dropped: {terms}"


def test_keyword_prefix_collisions_do_not_misroute() -> None:
    """Round-2 review: a leading-only \\b lets the keyword match as a word prefix.

    ``\\bpush`` hit "pushback" and ``\\bcommit`` hit "committee" — the same
    misrouting defect re-entering from the other side, and the git route's top
    priority means it wins.
    """
    for message in (
        "we got some pushback on the proposal from the client",
        "schedule the committee meeting for next week",
        "the bond issuer defaulted last quarter",
        "the creature escaped from the lab",
    ):
        decision = _resolve(message)
        assert decision.route_id == "direct", (
            f"{message!r} misrouted to {decision.route_id}, expected no keyword match"
        )


@pytest.mark.asyncio
async def test_short_token_does_not_degrade_whole_cjk_query(tmp_path: Path) -> None:
    """Round-2 review: one short token dragged the entire query onto the LIKE path.

    Chinese prose almost always carries punctuation or a two-character greeting,
    so this made the trigram slicing ineffective for most real input.
    """
    from engine.memory.search import SearchIndex

    idx = SearchIndex(tmp_path)
    await idx.open()
    try:
        await idx.index_entry(
            "ep1", "上次帮你把数据库查询做了索引优化，效果很好。", "episode"
        )
        for query in (
            "谢谢 数据库查询优化效果如何",
            "数据库查询优化，效果如何？",
        ):
            hits = await idx.search(query, top_k=5)
            assert hits, f"query with a short token must still match: {query!r}"
            assert hits[0]["id"] == "ep1"
    finally:
        await idx.close()


@pytest.mark.asyncio
async def test_kana_only_japanese_query_is_sliced(tmp_path: Path) -> None:
    """Round-2 review: the CJK class covered ideographs only, so kana stayed unsliced."""
    from engine.memory.search import SearchIndex

    idx = SearchIndex(tmp_path)
    await idx.open()
    try:
        await idx.index_entry("ep1", "きのうデータベースのさくいんをなおしました", "episode")
        hits = await idx.search("データベースのさくいんはどうなおしたのか", top_k=5)
        assert hits, "kana-only Japanese query must not return empty"
        assert hits[0]["id"] == "ep1"
    finally:
        await idx.close()


@pytest.mark.asyncio
async def test_unrelated_cjk_query_does_not_match(tmp_path: Path) -> None:
    """Broadening recall must not make every query match everything."""
    from engine.memory.search import SearchIndex

    idx = SearchIndex(tmp_path)
    await idx.open()
    try:
        await idx.index_entry("ep1", "用户上周要求优化数据库查询性能，已通过索引调整解决。", "episode")
        hits = await idx.search("周末去哪里吃火锅比较好", top_k=5)
        assert not hits, f"unrelated query should not match, got {hits}"
    finally:
        await idx.close()


# ── P1-14: MCP SSE size cap counts the whole stream, not one message ──

@pytest.mark.asyncio
async def test_sse_size_cap_is_per_message() -> None:
    """Many small SSE events must not accumulate into a false size-limit error."""
    from engine.mcp.client import MAX_MCP_RESPONSE_BYTES, _iter_sse_data_stream

    chunk = "x" * 8192
    event_count = (MAX_MCP_RESPONSE_BYTES // len(chunk)) + 8

    class FakeResponse:
        async def aiter_lines(self):
            for _ in range(event_count):
                yield f"data: {chunk}"
                yield ""

    seen = 0
    async for _payload in _iter_sse_data_stream(FakeResponse()):
        seen += 1
    assert seen == event_count, f"expected {event_count} events, drained {seen}"


@pytest.mark.asyncio
async def test_sse_size_cap_still_rejects_one_huge_message() -> None:
    """A genuinely oversized single message must still be rejected."""
    from engine.mcp.client import MAX_MCP_RESPONSE_BYTES, _iter_sse_data_stream

    class FakeResponse:
        async def aiter_lines(self):
            for _ in range(4):
                yield "data: " + "y" * (MAX_MCP_RESPONSE_BYTES // 2 + 16)

    with pytest.raises(RuntimeError, match="exceeds maximum size"):
        async for _payload in _iter_sse_data_stream(FakeResponse()):
            pass


# ── Second batch: P1-9 / P1-10 / P1-11 / P1-12 / P1-19 ──

def test_render_ui_rejects_huge_integers_gracefully() -> None:
    """A huge JSON integer must be refused, not crash the whole turn.

    math.isfinite() converts int to float and raises OverflowError past ~1.8e308,
    so a hallucinated value escaped smith_ui's "bounded structural validation"
    and failed the entire run instead of taking the graceful-reject path.
    """
    from engine.execution.react.smith_ui import validate_smith_ui_call

    call = {
        "spec": {
            "elements": [
                {"type": "Metric", "props": {"label": "x", "value": 10 ** 400}}
            ]
        }
    }
    result = validate_smith_ui_call(call, working_dir=None)
    assert not result.ok, "an unrepresentable number must be rejected"
    assert result.reason, "rejection must carry a reason"


def test_render_ui_still_accepts_ordinary_numbers() -> None:
    """The overflow guard must not reject normal values."""
    from engine.execution.react.smith_ui import _is_json_value

    for value in (0, 1, -1, 42, 3.14, 10 ** 18, -(10 ** 18)):
        assert _is_json_value(value), f"{value!r} should be valid"
    for value in (float("inf"), float("nan"), 10 ** 400):
        assert not _is_json_value(value), f"{value!r} should be invalid"


def test_openai_stream_surfaces_provider_error_chunk() -> None:
    """A provider error carried inside the stream must reach the caller.

    Relays report mid-stream failures as a `{"error": {...}}` chunk and then drop
    the connection; reading only `choices` replaced the real cause with
    "stream ended before the [DONE] sentinel".
    """
    from engine.llm.adapters.openai import _provider_error_message

    assert _provider_error_message(
        {"error": {"message": "rate limit exceeded", "type": "rate_limit"}}
    ) == "rate limit exceeded"
    assert _provider_error_message({"error": "upstream unavailable"}) == "upstream unavailable"
    assert _provider_error_message({"error": {"code": "content_filter"}}) == "content_filter"
    assert _provider_error_message({"error": {}}) == "unspecified provider error"
    # An ordinary content chunk must not be mistaken for a failure.
    assert _provider_error_message({"choices": [{"delta": {"content": "hi"}}]}) is None
    assert _provider_error_message({"error": None}) is None


def test_openai_non_stream_accepts_either_reasoning_field() -> None:
    """The non-streaming path must accept `reasoning` like the streaming one does.

    openai.py:167 already handles both spellings; the non-streaming branch read
    only reasoning_content, so relays using `reasoning` lost the content silently.
    """
    import inspect

    from engine.llm.adapters import openai as openai_adapter

    source = inspect.getsource(openai_adapter)
    non_stream = source.split("async def _stream_response")[0]
    assert 'choice.get("reasoning")' in non_stream, (
        "non-streaming path must fall back to the `reasoning` field"
    )


def test_usage_records_whether_the_provider_reported_anything() -> None:
    """A missing usage payload must be distinguishable from a genuine zero.

    Relays that omit `usage` made real, billed calls show up as 0 tokens and 0
    cost.  A round-5 review corrected my earlier claim that this needed a
    cross-layer change: no consumer sums the usage dict blindly, they all read
    named token keys, so an extra flag is safe.
    """
    from engine.llm.usage import USAGE_KEYS, normalize_usage

    absent = normalize_usage(None)
    assert absent["usage_reported"] == 0, "a missing payload must be flagged"

    real_zero = normalize_usage({"prompt_tokens": 0, "completion_tokens": 0})
    assert real_zero["usage_reported"] == 1, "a real payload must be flagged reported"
    assert real_zero["input_tokens"] == 0, "a reported zero stays zero"

    # The flag must stay out of the token key tuple so token-iterating consumers
    # (projections, token_stats) are unaffected.
    assert "usage_reported" not in USAGE_KEYS


def test_trace_redacts_credentials_embedded_in_values() -> None:
    """Field-name matching misses secrets carried inside a value.

    TOOL_CALL_START writes the full tool arguments, so a shell command holding a
    bearer token landed in the trace verbatim — the key is "command", which does
    not trigger the name-based rule.
    """
    from engine.observability.trace_store import _bounded_trace_value

    secret = "sk-ant-api03-must-not-appear"
    payload = {
        "name": "shell",
        "arguments": {
            "command": f"curl -H 'Authorization: Bearer {secret}' https://example.test",
        },
    }
    rendered = str(_bounded_trace_value(payload))
    assert secret not in rendered, f"credential leaked into the trace: {rendered}"


def test_trace_keeps_ordinary_values_intact() -> None:
    """Value scanning must not mangle normal command text."""
    from engine.observability.trace_store import _bounded_trace_value

    payload = {"arguments": {"command": "pytest -q tests/", "cwd": "/repo"}}
    rendered = _bounded_trace_value(payload)
    assert rendered["arguments"]["command"] == "pytest -q tests/"
    assert rendered["arguments"]["cwd"] == "/repo"


# ── Third batch: P1-2 / P1-16 / P1-17 ──

def test_colliding_mcp_tool_names_are_all_registered() -> None:
    """Names that fold together must all survive registration.

    `search-docs` and `search_docs` both clean to `search_docs`, and the loser hit
    register()'s duplicate-name error and vanished from the session behind a
    warning — the model silently lost a tool.  Cleaning stays lossy on purpose
    (`safe-tool` is meant to become `safe_tool`); only the collision is suffixed.
    """
    import asyncio

    from engine.mcp.client import MAX_TOOL_NAME_LENGTH, MCPTool, register_mcp_tools_with_prefix
    from engine.tool.registry import ToolRegistry

    class FakeClient:
        async def list_tools(self):
            return [MCPTool("search-docs", "", {}), MCPTool("search_docs", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        count = await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp")
        return count, sorted(tool.name for tool in registry.list_tools())

    count, names = asyncio.run(run())
    assert count == 2, f"both tools must register, got {count}: {names}"
    assert len(set(names)) == 2, f"names must stay distinct: {names}"
    assert "mcp_search_docs" in names, f"the first name keeps its spelling: {names}"
    assert all(len(name) <= MAX_TOOL_NAME_LENGTH for name in names), names


def test_non_colliding_mcp_tool_name_keeps_its_spelling() -> None:
    """Dedup must not disturb the ordinary case (existing contract)."""
    import asyncio

    from engine.mcp.client import MCPTool, register_mcp_tools_with_prefix
    from engine.tool.registry import ToolRegistry

    class FakeClient:
        async def list_tools(self):
            return [MCPTool("safe-tool", "", {})]

        async def call_tool(self, name, arguments):
            return name

    async def run():
        registry = ToolRegistry()
        await register_mcp_tools_with_prefix(registry, FakeClient(), prefix="mcp_docs")
        return [tool.name for tool in registry.list_tools()]

    assert asyncio.run(run()) == ["mcp_docs_safe_tool"]


def test_backtrack_event_carries_the_failure_reason() -> None:
    """Backtracking must tell the target node why it was sent back.

    Without it the target re-runs against the original request with no signal
    about what failed, so it reproduces the same output — and FailureLoopGuard
    counts per skill without resetting, so that is the only correction the
    pipeline ever gets.

    Source-level assertion, not behavioural: driving a real double-gate-failure
    backtrack needs a chain plus stub LLM and gates. Stated plainly so nobody
    mistakes this for proof that the hint reaches the target node.
    """
    import inspect

    from engine.execution.pipeline import pipeline as pipeline_module

    lines = inspect.getsource(pipeline_module).splitlines()
    backtrack_at = next(
        index for index, line in enumerate(lines) if "EventType.BACKTRACK" in line
    )
    emitted = "\n".join(lines[backtrack_at:backtrack_at + 6])
    assert '"reason"' in emitted, f"BACKTRACK must carry the gate reason:\n{emitted}"

    # The hint must be assigned *before* the jump, mirroring the retry branch.
    hint_assignments = [
        index
        for index, line in enumerate(lines)
        if "CTX_RETRY_HINT] = gate_result.retry_hint" in line
    ]
    assert hint_assignments, "no retry-hint assignment found at all"
    assert any(index < backtrack_at for index in hint_assignments), (
        "the retry hint is only set on the retry path, so a backtrack still runs blind"
    )


def test_tool_provider_contract_error_is_logged_visibly(tmp_path: Path, caplog) -> None:
    """A duplicate tool name must not disappear with only a debug-level trace."""
    import logging

    from engine.tool.registry import ToolRegistry

    tools = tmp_path / "tools"
    tools.mkdir()
    body = (
        'TOOL_META = {{"name": "dup_tool", "description": "d", "parameters": {{}}}}\n'
        "async def execute(**kwargs):\n"
        "    return {!r}\n"
    )
    (tools / "a_first.py").write_text(body.format("first"))
    (tools / "b_second.py").write_text(body.format("second"))

    registry = ToolRegistry()
    with caplog.at_level(logging.ERROR):
        registry.load_providers(tools)

    assert any(
        "unavailable" in record.message or "unavailable" in record.getMessage()
        for record in caplog.records
    ), f"the dropped tool must be reported at ERROR: {[r.getMessage() for r in caplog.records]}"


# ── P1-15: approval redaction is weaker than the audit-log redaction ──

def test_approval_redacts_credential_name_variants() -> None:
    """Approval cards must redact the same key variants tool_guard redacts."""
    from engine.safety.approval import summarize_arguments

    secret = "sk-live-must-not-appear"
    arguments = {
        "password": secret,
        "db_password": secret,
        "clientSecret": secret,
        "auth_token": secret,
        "webhook_secret": secret,
        "signing_secret": secret,
        "API-Key": secret,
        "refreshToken": secret,
    }
    summary = summarize_arguments(arguments)
    leaked = [key for key, value in summary.items() if secret in str(value)]
    assert not leaked, f"credentials leaked into the approval summary: {leaked}"


def test_approval_redacts_nested_credentials() -> None:
    """Nested mappings must be redacted too, not just top-level keys."""
    from engine.safety.approval import summarize_arguments

    secret = "sk-live-nested"
    summary = summarize_arguments({"headers": {"Authorization": secret}})
    assert secret not in str(summary), f"nested credential leaked: {summary}"


# ── P1-6: session checkpoint had no owning run, so a live run's state got adopted ──

def test_checkpoint_without_run_id_is_not_resumed(tmp_path: Path) -> None:
    """A legacy checkpoint carries no owner and must not be adopted."""
    from engine.execution.orchestration.agent_loop import _checkpoint_owner_still_running

    assert not _checkpoint_owner_still_running(str(tmp_path), "", "current")


def test_checkpoint_owned_by_a_live_run_is_not_adopted(tmp_path: Path) -> None:
    """Re-submitting the same message must not steal a still-running run's state."""
    from engine.execution.orchestration.agent_loop import _checkpoint_owner_still_running
    from engine.execution.orchestration.run_state import RunStateStore

    store = RunStateStore(tmp_path)
    store.create("ownerrun0000000000000000000001", agent_id="smith-id", session_id="s1")

    assert _checkpoint_owner_still_running(
        str(tmp_path), "ownerrun0000000000000000000001", "currentrun00000000000000000001"
    ), "a RUNNING owner must block adoption"


def test_checkpoint_owned_by_a_finished_run_is_adopted(tmp_path: Path) -> None:
    """A crashed run reconciled out of RUNNING must still be resumable.

    Startup calls RunStateStore.recover_interrupted(), so a genuinely crashed run
    is no longer RUNNING by the time anything tries to resume it — otherwise this
    check would have disabled crash recovery altogether.
    """
    from engine.execution.orchestration.agent_loop import _checkpoint_owner_still_running

    assert not _checkpoint_owner_still_running(
        str(tmp_path), "goneruns0000000000000000000001", "currentrun00000000000000000001"
    ), "an unknown/finished owner must not block adoption"


# ── Round-4 review: the P1-7 fix reused CommandResult.error as a warning ──

def test_incomplete_output_is_not_reported_as_execution_failure() -> None:
    """A truncated drain must not discard the exit code and captured output.

    shell.py treats any ``error`` as "the command failed" and returns early, so
    folding "output may be incomplete" into that field turned a successful build
    into a bare error string.
    """
    from engine.sandbox.host import CommandResult

    result = CommandResult(
        exit_code=0,
        stdout="build finished: 42 tests passed",
        output_incomplete=True,
    )
    assert result.error is None, "an incomplete drain is not an execution error"
    assert result.output_incomplete
    assert result.exit_code == 0
    assert "42 tests passed" in result.stdout


@pytest.mark.asyncio
async def test_abandoned_drain_reports_the_real_byte_total() -> None:
    """The abandoned-drain path must keep the true total, or truncation is hidden.

    Reporting len(retained) as the total makes _format_stream's
    "truncated, N bytes total" note vanish exactly when output was dropped.
    """
    import asyncio

    from engine.sandbox.host import MAX_OUTPUT, _read_limited, _StreamBuffer

    stream = asyncio.StreamReader()
    payload = b"X" * (MAX_OUTPUT * 2)
    stream.feed_data(payload)
    buffer = _StreamBuffer()
    task = asyncio.create_task(_read_limited(stream, buffer))
    await asyncio.sleep(0)  # let the reader drain what is already buffered
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    retained, total = buffer.value()
    assert len(retained) <= MAX_OUTPUT
    assert total == len(payload), f"expected real total {len(payload)}, got {total}"
    assert total > len(retained), "truncation must remain visible"


def test_live_owner_checkpoint_is_kept_not_cleared(tmp_path: Path) -> None:
    """Refusing to adopt a live run's checkpoint must not delete it.

    The refusal previously fell through to the same clear() as a stale
    checkpoint, destroying the crash-recovery point of a run still executing.
    """
    from engine.execution.orchestration.agent_loop import _apply_crash_checkpoint
    from engine.execution.orchestration.run_state import RunStateStore
    from engine.execution.pipeline.checkpoint import SessionCheckpoint, SessionStateManager

    owner = "liveowner00000000000000000000001"
    RunStateStore(tmp_path).create(owner, agent_id="smith-id", session_id="s-live")

    manager = SessionStateManager(tmp_path)
    manager.save(SessionCheckpoint(
        run_id=owner,
        agent_id="smith-id",
        session_id="s-live",
        identity_id="smith",
        route_id="feature",
        skill_chain_index=0,
        context={"user_message": "build a feature"},
        timestamp="2026-07-28T00:00:00+00:00",
        working_dir=str(tmp_path.resolve()),
    ))

    context, start = _apply_crash_checkpoint(
        {
            "agent_id": "smith-id",
            "identity_id": "smith",
            "session_id": "s-live",
            "_state_dir": str(tmp_path),
            "_working_dir": str(tmp_path.resolve()),
            "_run_id": "otherrun000000000000000000000002",
        },
        "feature",
        "build a feature",
        2,
    )
    assert start == 0, "a live owner's checkpoint must not be adopted"
    assert manager.restore("s-live") is not None, "the live run's checkpoint was deleted"


def test_gitdir_pointer_to_arbitrary_directory_is_ignored(tmp_path: Path) -> None:
    """An untrusted .git file must not aim the hard-link scan at any path.

    The .git pointer file is covered by no write guard — it arrives with a cloned
    repo — so following it unvalidated turns every nlink>1 write check into a
    walk of whatever it names.
    """
    from engine.safety.tool_guard import _git_dirs_for

    decoy = tmp_path / "not-a-git-dir"
    (decoy / "hooks").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {decoy}\n")

    assert _git_dirs_for(work) == [], "a pointer to a non-git directory must be ignored"

    real = tmp_path / "real.git"
    (real / "hooks").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/main\n")
    (work / ".git").write_text(f"gitdir: {real}\n")
    assert _git_dirs_for(work) == [real.resolve()], "a real git dir must still resolve"


def test_approval_keeps_ordinary_arguments_readable() -> None:
    """Redaction must not swallow non-sensitive arguments."""
    from engine.safety.approval import summarize_arguments

    summary = summarize_arguments({"path": "src/main.py", "line_count": 42})
    assert summary["path"] == "src/main.py"
    assert summary["line_count"] == 42
