from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.llm.client import ChatResponse
from engine.memory.compile import (
    MemoryCompilationError,
    assemble_memory,
    compile_context,
    compile_durable,
    compile_recent,
)
from engine.memory.policy import (
    MemoryPolicyError,
    load_memory_policy,
    resolve_view_path,
    validate_rendered_view,
)
from engine.memory.store import save_conversation_memory, search_relevant_memories
from engine.memory.user_learner import UserPreferenceLearner


CONTEXT_DOC = """# Smith Context

## Confirmed Preferences
- **Language**: Default to Chinese.

## Collaboration Patterns
- **Answers**: Lead with the conclusion.

## Stable User Context
"""

RECENT_DOC = """# Recent Working Memory

## Active Work
- **Memory upgrade** — 状态：implementing；下一步：run tests；更新：2026-07-13。

## Pending

## Recent Verified Outcomes
- **Policy** — 结果：policy parsed；证据：unit test。
"""

DURABLE_DOC = """# Durable Project Memory

## Confirmed Facts
- **Storage**: Memory views are Markdown files.

## Decisions
- **Policy**: 决定 use one shared MemoryPolicy；适用范围：memory compilation。

## Reusable Procedures
- **Compilation**: Generate, review, then atomically write；验证：pipeline test。

## Known Pitfalls
- **Free-form summaries**: 避免 appending raw answers；原因：they pollute recall。
"""

EMPTY_DURABLE_DOC = """# Durable Project Memory

## Confirmed Facts

## Decisions

## Reusable Procedures

## Known Pitfalls
"""


class StaticLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
        self.calls.append(messages)
        return ChatResponse(text=self.text)


class PassReviewer(StaticLLM):
    def __init__(self) -> None:
        super().__init__('{"pass": true, "hard_fail": [], "soft_fail": [], "feedback": ""}')


def _write_event(memory_dir: Path, **overrides: object) -> None:
    event = {
        "task": "upgrade Smith memory",
        "summary": "policy implementation is in progress",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "work",
        "scope": "project",
        "evidence": "tool_result",
    }
    event.update(overrides)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "recent.jsonl").write_text(
        json.dumps(event, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_memory_policy_loads_one_canonical_three_view_contract(tmp_path: Path) -> None:
    policy = load_memory_policy()

    assert policy.version == 2
    assert set(policy.views) == {"context", "recent", "durable"}
    assert resolve_view_path(policy, tmp_path, "context") == tmp_path / "context.md"
    assert resolve_view_path(policy, tmp_path, "recent") == tmp_path / "memory" / "recent.md"
    assert "只写未来对话仍可能有用的信息" in policy.instructions_for("recent", role="compiler")
    assert "## Recent Verified Outcomes" in policy.instructions_for(
        "recent", role="compiler"
    )
    assert "## Confirmed Facts" not in policy.instructions_for(
        "recent", role="compiler"
    )
    assert "Reviewer" in policy.instructions_for("recent", role="reviewer")


def test_memory_policy_rejects_wrong_or_extra_markdown_sections() -> None:
    policy = load_memory_policy()

    validate_rendered_view(policy, "recent", RECENT_DOC)

    with pytest.raises(MemoryPolicyError, match="title"):
        validate_rendered_view(policy, "recent", RECENT_DOC.replace("# Recent Working Memory", "# Notes"))

    with pytest.raises(MemoryPolicyError, match="sections"):
        validate_rendered_view(policy, "recent", RECENT_DOC + "\n## Random Notes\n- noise\n")


def test_memory_policy_is_included_in_engine_package_data() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    package_data = config["tool"]["setuptools"]["package-data"]
    assert "MEMORY_POLICY.md" in package_data["engine.memory"]


def test_smith_context_seed_matches_memory_policy() -> None:
    seed = Path(__file__).resolve().parents[3] / "agents" / "smith" / "context.md"

    validate_rendered_view(
        load_memory_policy(),
        "context",
        seed.read_text(encoding="utf-8"),
    )


def test_explicit_toolless_preference_is_recorded_as_evidence(tmp_path: Path) -> None:
    asyncio.run(save_conversation_memory(
        tmp_path,
        "以后默认用中文回答",
        "好的",
        had_tools=False,
    ))

    event = json.loads((tmp_path / "memory" / "recent.jsonl").read_text(encoding="utf-8"))
    assert event["kind"] == "preference"
    assert event["scope"] == "user"
    assert event["evidence"] == "user_explicit"


def test_preference_learner_emits_evidence_without_directly_writing_context(tmp_path: Path) -> None:
    context_path = tmp_path / "context.md"
    original = "# User-authored context\n\nKeep this unchanged.\n"
    context_path.write_text(original, encoding="utf-8")
    learner = UserPreferenceLearner(tmp_path)

    async def run() -> list[str]:
        observations: list[str] = []
        for _ in range(3):
            observations.extend(await learner.observe("async coroutine design", "reply"))
        return observations

    observations = asyncio.run(run())

    assert "tech_level=expert" in observations
    assert context_path.read_text(encoding="utf-8") == original
    learner.acknowledge(observations)
    assert asyncio.run(learner.observe("async coroutine design", "reply")) == []


def test_repeated_learning_signal_reaches_context_compiler(tmp_path: Path) -> None:
    asyncio.run(save_conversation_memory(
        tmp_path,
        "async coroutine design",
        "reply",
        had_tools=False,
        learning_signals=["tech_level=expert"],
    ))
    memory_dir = tmp_path / "memory"
    generator = StaticLLM(CONTEXT_DOC)

    assert asyncio.run(
        compile_context(memory_dir, generator, PassReviewer())
    ) is True
    assert "signals=['tech_level=expert']" in generator.calls[0][-1]["content"]


def test_formal_memory_view_requires_reviewer_before_write(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(memory_dir)

    with pytest.raises(MemoryCompilationError, match="requires a reviewer"):
        asyncio.run(compile_recent(memory_dir, StaticLLM(RECENT_DOC)))

    assert not (memory_dir / "recent.md").exists()
    history = json.loads((memory_dir / "memory_history.jsonl").read_text(encoding="utf-8"))
    assert history["status"] == "rejected"


def test_compile_context_uses_policy_review_and_audit_history(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(
        memory_dir,
        task="以后默认用中文回答",
        summary="user preference acknowledged",
        kind="preference",
        scope="user",
        evidence="user_explicit",
    )
    generator = StaticLLM(CONTEXT_DOC)
    reviewer = PassReviewer()

    assert asyncio.run(compile_context(memory_dir, generator, reviewer)) is True

    assert (tmp_path / "context.md").read_text(encoding="utf-8") == CONTEXT_DOC
    assert "context.md" in generator.calls[0][-1]["content"]
    assert "Smith Memory Policy" in reviewer.calls[0][-1]["content"]
    history = json.loads((memory_dir / "memory_history.jsonl").read_text(encoding="utf-8"))
    assert history["target"] == "context"
    assert history["status"] == "written"
    assert history["review_rounds"] == 1


def test_compile_recent_writes_only_policy_structured_markdown(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(memory_dir)
    generator = StaticLLM(RECENT_DOC)
    reviewer = PassReviewer()

    assert asyncio.run(compile_recent(memory_dir, generator, reviewer)) is True

    assert (memory_dir / "recent.md").read_text(encoding="utf-8") == RECENT_DOC
    assert "Recent Working Memory" in generator.calls[0][-1]["content"]
    assert "Smith Memory Policy" in reviewer.calls[0][-1]["content"]


def test_compile_durable_creates_empty_template_without_events(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    generator = StaticLLM(DURABLE_DOC)

    assert asyncio.run(compile_durable(memory_dir, generator, PassReviewer())) is False
    assert (memory_dir / "durable.md").read_text(encoding="utf-8") == EMPTY_DURABLE_DOC
    assert generator.calls == []


def test_generic_work_events_never_become_durable_memory_candidates(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(memory_dir, kind="work", scope="project")

    assert asyncio.run(
        compile_durable(memory_dir, StaticLLM(DURABLE_DOC), PassReviewer())
    ) is False
    assert (memory_dir / "durable.md").read_text(encoding="utf-8") == EMPTY_DURABLE_DOC


def test_explicit_stable_decision_remains_a_durable_memory_candidate(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(memory_dir, kind="decision", scope="project", evidence="user_explicit")

    assert asyncio.run(
        compile_durable(memory_dir, StaticLLM(DURABLE_DOC), PassReviewer())
    ) is True
    assert (memory_dir / "durable.md").read_text(encoding="utf-8") == DURABLE_DOC


def test_compile_recent_rejects_free_form_output_and_keeps_old_view(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(memory_dir)
    old = RECENT_DOC.replace("implementing", "old state")
    (memory_dir / "recent.md").write_text(old, encoding="utf-8")

    with pytest.raises(MemoryPolicyError, match="title"):
        asyncio.run(
            compile_recent(
                memory_dir,
                StaticLLM("free-form summary"),
                PassReviewer(),
            )
        )

    assert (memory_dir / "recent.md").read_text(encoding="utf-8") == old
    history = json.loads((memory_dir / "memory_history.jsonl").read_text(encoding="utf-8"))
    assert history["target"] == "recent"
    assert history["status"] == "rejected"


def test_compile_durable_accepts_complete_view_without_adding_legacy_wrapper(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    _write_event(
        memory_dir,
        kind="decision",
        scope="project",
        evidence="user_explicit",
    )

    assert asyncio.run(compile_durable(memory_dir, StaticLLM(DURABLE_DOC), PassReviewer())) is True

    content = (memory_dir / "durable.md").read_text(encoding="utf-8")
    assert content == DURABLE_DOC
    assert content.count("# Durable Project Memory") == 1
    assert "## Durable Memory" not in content


def test_assemble_memory_can_exclude_durable_for_query_time_recall(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "recent.md").write_text(RECENT_DOC, encoding="utf-8")
    (memory_dir / "durable.md").write_text(DURABLE_DOC, encoding="utf-8")

    assembled = assemble_memory(memory_dir, include_durable=False)

    assert "Recent Working Memory" in assembled
    assert "Durable Project Memory" not in assembled


def test_query_time_recall_reads_matching_durable_without_episode_directory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "durable.md").write_text(
        DURABLE_DOC.replace("Memory views are Markdown files", "PostgreSQL is the project database"),
        encoding="utf-8",
    )

    result = asyncio.run(search_relevant_memories(tmp_path, "PostgreSQL database migration"))

    assert "## Relevant Durable Memory" in result
    assert "PostgreSQL is the project database" in result
    assert "Free-form summaries" not in result


def test_query_time_recall_degrades_when_durable_reader_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_reader(*_args: object, **_kwargs: object) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr("engine.memory.store._select_relevant_durable", fail_reader)

    assert asyncio.run(search_relevant_memories(tmp_path, "anything")) == ""
