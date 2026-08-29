"""Regressions for the 2026-08-10 engine review.

One check per non-trivial fix; each fails if that fix is reverted.
"""
from __future__ import annotations

from engine.context.budget import estimate_tokens
from engine.context.compression import (
    RUNTIME_USER_NOTE_PREFIX,
    _split_active_context,
)
from engine.context.summary import _has_required_summary_structure
from engine.execution.react.budget import (
    CONVERSATION_HARD_LIMIT,
    trim_conversation_to_message_cap,
)


def test_injected_clock_note_is_not_the_active_request():
    """get_current_time appends a user turn; it must not displace the request.

    Treating it as the active turn pushed the real instruction into
    compactable history, where compact_history replaced it with a summary.
    """
    conversation = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "重构 X 为 Y，别动测试"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ack"},
        {"role": "user", "content": f"{RUNTIME_USER_NOTE_PREFIX}2026-08-10T14:00"},
        {"role": "assistant", "content": "好的"},
    ]
    _protected, history, active = _split_active_context(conversation)

    assert "重构 X 为 Y" in active[0]["content"]
    assert not any("重构 X 为 Y" in str(m.get("content", "")) for m in history)


def test_wide_char_estimate_covers_kana_hangul_and_cjk_punctuation():
    """Kana/Hangul/CJK punctuation cost ~1 token, not 1/3.

    Every context budget derives from this number, so a 3x understatement on
    Japanese or Korean input silently overfills the request.
    """
    for sample in ("こんにちは", "안녕하세요", "。、「」；：", "㐀㐁㐂"):
        per_char = estimate_tokens(sample) / len(sample)
        assert per_char >= 1.0, (sample, per_char)

    # ASCII is unchanged: still ~1 token per 3 characters.
    assert estimate_tokens("a" * 300) == 100


def test_summary_structure_check_refuses_a_declared_entity_document():
    """ElementTree expands internal entities; a DTD is a bomb, not a summary."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r ['
        '<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        ']><context_summary><conversation_overview>&b;</conversation_overview>'
        "<key_knowledge/><file_system_state/><recent_actions/><current_plan/>"
        "</context_summary>"
    )
    assert _has_required_summary_structure(bomb) is False

    plain = (
        "<context_summary><conversation_overview>ok</conversation_overview>"
        "<key_knowledge/><file_system_state/><recent_actions/><current_plan/>"
        "</context_summary>"
    )
    assert _has_required_summary_structure(plain) is True


def _orphan_tool_messages(messages: list[dict]) -> int:
    open_ids: set[str] = set()
    orphans = 0
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            open_ids.update(call["id"] for call in message["tool_calls"])
        elif message.get("role") == "tool" and message.get("tool_call_id") not in open_ids:
            orphans += 1
    return orphans


def test_hard_limit_still_trims_when_a_later_boundary_exists():
    """A long tool round must not silently disable the whole hard limit.

    Backing off to a round boundary collapsed onto CONVERSATION_KEEP_HEAD once
    ~8 parallel calls sat behind the head, making head+tail the original
    conversation.  Scanning forward instead drops the whole round together with
    its assistant, so the bound holds and pairing survives.

    The unsplittable case (nothing but tool results after the cut) keeps the
    conversation intact on purpose — see
    test_hard_limit_prefers_valid_pairing_over_truncation in test_react_budget.
    """
    calls = 8
    conversation: list[dict] = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call-{i}"} for i in range(calls)],
        },
    ]
    for i in range(calls):
        conversation.append({"role": "tool", "tool_call_id": f"call-{i}", "content": "r"})
        conversation.append({"role": "system", "content": "recovery hint"})
    while len(conversation) <= CONVERSATION_HARD_LIMIT:
        conversation.append({"role": "assistant", "content": "pad"})

    trimmed = trim_conversation_to_message_cap(conversation)

    assert len(trimmed) < len(conversation)
    assert _orphan_tool_messages(trimmed) == 0
    assert trimmed[0]["content"] == "contract"
    assert trimmed[1]["content"] == "question"
