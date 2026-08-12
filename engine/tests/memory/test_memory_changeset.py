"""The applier's job is that nothing happens to what no change names."""

from __future__ import annotations

from engine.memory._changeset import (
    EVICTION_ORDER,
    apply_changes,
    evict_to_budget,
    parse_changeset,
    parse_document,
    render_document,
    topic_key,
)

DURABLE_SECTIONS = (
    "Active Work",
    "Pending",
    "Verified Outcomes",
    "Decisions",
    "Known Pitfalls",
)

EXISTING = """# Durable Project Memory

## Active Work
- **记忆改造** — 状态：进行中；下一步：写应用器；更新：2026-08-12。
- **门禁重构** — 状态：待验证；下一步：跑测试；更新：2026-08-11。

## Pending

## Verified Outcomes
- **快照落地** — 结果：git 快照可回滚；证据：test_memory_snapshot。

## Decisions
- **输出格式**: 决定 编译输出变更集；适用范围：两个视图。

## Known Pitfalls
"""


def _parse(payload):
    return parse_changeset(payload, view="durable", sections=DURABLE_SECTIONS)


def test_bullets_no_change_names_are_untouched():
    """The whole point of a change set: forgetting to copy a line is impossible."""
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    changes, rejected, _ = _parse(
        {
            "changes": [
                {
                    "op": "remove",
                    "section": "Active Work",
                    "target": "**门禁重构**",
                    "reason": "已完成",
                    "evidence": {"ref": "2026-08-12T05:02:10Z", "quote": "测试通过"},
                }
            ]
        }
    )
    assert rejected == []

    result, applied, rejects = apply_changes(grouped, changes)
    assert len(applied) == 1
    assert rejects == []

    document = render_document("Durable Project Memory", DURABLE_SECTIONS, result)
    assert "**门禁重构**" not in document
    # Everything else survives without being restated by the model.
    assert "**记忆改造**" in document
    assert "**快照落地**" in document
    assert "**输出格式**" in document


def test_one_bad_change_does_not_sink_the_good_ones():
    """Partial success: the whole-document compiler had to discard all ten."""
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    changes, _, _ = _parse(
        {
            "changes": [
                {
                    "op": "add",
                    "section": "Decisions",
                    "content": "- **淘汰顺序**: 决定 按策略 5.1 顺序；适用范围：两个视图。",
                    "evidence": {"ref": "2026-08-12T06:00:00Z", "quote": "确认"},
                },
                {
                    "op": "remove",
                    "section": "Active Work",
                    "target": "**根本不存在的条目**",
                },
                {
                    "op": "add",
                    "section": "Known Pitfalls",
                    "content": "- **整篇重写**: 避免 漏抄等于删除；原因：已验证。",
                    "evidence": {"ref": "2026-08-12T06:00:01Z", "quote": "确认"},
                },
            ]
        }
    )

    result, applied, rejects = apply_changes(grouped, changes)
    assert len(applied) == 2
    assert [r.reason for r in rejects] == ["target_not_found"]

    document = render_document("Durable Project Memory", DURABLE_SECTIONS, result)
    assert "**淘汰顺序**" in document
    assert "**整篇重写**" in document


def test_duplicate_add_is_rejected_not_duplicated():
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    changes, _, _ = _parse(
        {
            "changes": [
                {
                    "op": "add",
                    "section": "Active Work",
                    "content": "- **记忆改造** — 状态：又写一遍；下一步：无；更新：2026-08-12。",
                    "evidence": {"ref": "2026-08-12T06:00:00Z", "quote": "x"},
                }
            ]
        }
    )
    _, applied, rejects = apply_changes(grouped, changes)
    assert applied == []
    assert [r.reason for r in rejects] == ["topic_already_exists"]


def test_ambiguous_target_is_refused_not_guessed():
    """Editing the wrong memory is worse than not editing at all."""
    doubled = EXISTING.replace(
        "## Pending\n",
        "## Pending\n- **同名** — 待处理：第一条。\n- **同名** — 待处理：第二条。\n",
    )
    grouped = parse_document(doubled, DURABLE_SECTIONS)
    changes, _, _ = _parse(
        {"changes": [{"op": "remove", "section": "Pending", "target": "**同名**"}]}
    )
    _, applied, rejects = apply_changes(grouped, changes)
    assert applied == []
    assert rejects[0].reason == "target_is_ambiguous"
    assert "2 matches" in rejects[0].detail


def test_replace_may_not_rename_the_topic_key():
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    changes, _, _ = _parse(
        {
            "changes": [
                {
                    "op": "replace",
                    "section": "Active Work",
                    "target": "**记忆改造**",
                    "content": "- **改了名字** — 状态：x；下一步：y；更新：2026-08-12。",
                    "evidence": {"ref": "2026-08-12T06:00:00Z", "quote": "x"},
                }
            ]
        }
    )
    _, applied, rejects = apply_changes(grouped, changes)
    assert applied == []
    assert rejects[0].reason == "replace_changes_topic_key"


def test_replace_updates_in_place():
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    changes, _, _ = _parse(
        {
            "changes": [
                {
                    "op": "replace",
                    "section": "Active Work",
                    "target": "**记忆改造**",
                    "content": "- **记忆改造** — 状态：应用器已写；下一步：接线；更新：2026-08-12。",
                    "evidence": {"ref": "2026-08-12T06:00:00Z", "quote": "x"},
                }
            ]
        }
    )
    result, applied, rejects = apply_changes(grouped, changes)
    assert len(applied) == 1 and rejects == []
    assert "应用器已写" in result["Active Work"][0]
    # Position is preserved: a replace is an in-place edit, not remove + append.
    assert topic_key(result["Active Work"][0]) == "记忆改造"


def test_structural_rejects_never_reach_the_applier():
    changes, rejected, _ = _parse(
        {
            "changes": [
                {"op": "delete", "section": "Decisions", "content": "- **x**: y"},
                {"op": "add", "section": "Nonexistent", "content": "- **x**: y"},
                {"op": "add", "section": "Decisions", "content": "no topic key"},
                {"op": "remove", "section": "Decisions"},
                {
                    "op": "add",
                    "view": "context",
                    "section": "Decisions",
                    "content": "- **x**: y",
                },
            ]
        }
    )
    assert changes == []
    assert [r.reason for r in rejected] == [
        "unknown_op",
        "unknown_section",
        "content_has_no_topic_key",
        "missing_target",
        "wrong_view",
    ]


def test_nothing_to_record_is_reported_separately_from_failure():
    """A quiet stretch genuinely has nothing to remember."""
    changes, rejected, nothing = _parse({"changes": [], "nothing_to_record": True})
    assert changes == [] and rejected == [] and nothing is True

    # Zero changes without the flag is not the same statement.
    changes, rejected, nothing = _parse({"changes": []})
    assert nothing is False


def test_malformed_payload_is_a_reject_not_a_crash():
    for payload in ("not json", ["a", "list"], 42, None):
        changes, rejected, nothing = _parse(payload)
        assert changes == [] and nothing is False
        assert rejected[0].reason == "changeset_not_an_object"

    changes, rejected, _ = _parse({"changes": "not a list"})
    assert rejected[0].reason == "changes_not_a_list"


def test_eviction_drops_whole_bullets_in_policy_order():
    grouped = {
        "Active Work": [
            f"- **工作{i}** — 状态：x；下一步：y；更新：2026-08-12。" for i in range(6)
        ],
        "Pending": ["- **待办** — 待处理：x。"],
        "Verified Outcomes": ["- **结果** — 结果：x；证据：y。"],
        "Decisions": ["- **决定**: 决定 x；适用范围：y。"],
        "Known Pitfalls": ["- **陷阱**: 避免 x；原因：y。"],
    }
    document, evicted = evict_to_budget(
        grouped,
        title="Durable Project Memory",
        sections=DURABLE_SECTIONS,
        order=EVICTION_ORDER["durable"],
        max_chars=320,
    )
    assert len(document) <= 320
    assert evicted, "budget was exceeded, something had to go"
    # Active Work is evicted first, and the oldest bullet in it goes first.
    assert topic_key(evicted[0]) == "工作0"
    # No bullet is ever cut in half.
    for line in document.splitlines():
        if line.startswith("- "):
            assert line.rstrip().endswith("。")
    # The entries worth keeping longest survive.
    assert "**决定**" in document
    assert "**陷阱**" in document


def test_parse_and_render_round_trip():
    grouped = parse_document(EXISTING, DURABLE_SECTIONS)
    rendered = render_document("Durable Project Memory", DURABLE_SECTIONS, grouped)
    assert rendered.strip() == EXISTING.strip()


def test_content_under_unknown_heading_is_dropped():
    text = EXISTING + "\n## 杂项\n- **偷渡的条目** — 状态：x。\n"
    grouped = parse_document(text, DURABLE_SECTIONS)
    assert all(
        "偷渡的条目" not in bullet for bullets in grouped.values() for bullet in bullets
    )


def test_context_view_uses_its_own_eviction_order():
    sections = ("Confirmed Preferences", "Collaboration Patterns", "Stable User Context")
    grouped = {
        "Confirmed Preferences": ["- **回复语言**: 中文。"],
        "Collaboration Patterns": ["- **审查**: 先审后修。"],
        "Stable User Context": [f"- **背景{i}**: x。" for i in range(4)],
    }
    document, evicted = evict_to_budget(
        grouped,
        title="Smith Context",
        sections=sections,
        order=EVICTION_ORDER["context"],
        max_chars=150,
    )
    assert len(document) <= 150
    # Background goes first; an explicit stated preference is evicted last.
    assert topic_key(evicted[0]) == "背景0"
    assert "**回复语言**" in document
