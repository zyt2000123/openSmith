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
    (main_repo / ".git" / "worktrees" / "wt").mkdir(parents=True)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {main_repo / '.git' / 'worktrees' / 'wt'}\n"
    )
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


def test_approval_keeps_ordinary_arguments_readable() -> None:
    """Redaction must not swallow non-sensitive arguments."""
    from engine.safety.approval import summarize_arguments

    summary = summarize_arguments({"path": "src/main.py", "line_count": 42})
    assert summary["path"] == "src/main.py"
    assert summary["line_count"] == 42
