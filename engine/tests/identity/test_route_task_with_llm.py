from __future__ import annotations

import asyncio
from pathlib import Path

from engine.execution.routing.task_router import route_task_with_llm
from engine.identity import IdentityCatalog
from engine.llm.contracts import ChatResponse


def _write_identity(directory: Path, name: str, *lines: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("\n".join(lines), encoding="utf-8")


def _catalog(tmp_path: Path) -> IdentityCatalog:
    _write_identity(
        tmp_path,
        "smith.yaml",
        "schema: agentsmith.identity/v1",
        "id: smith",
        "name: Smith",
        "default: true",
        "routes: []",
    )
    _write_identity(
        tmp_path,
        "coding.yaml",
        "schema: agentsmith.identity/v1",
        "id: coding",
        "name: Coding",
        "routes:",
        "  - id: requirements-research",
        "    keywords: [需求调研]",
        "    pipeline: requirements-research",
        "  - id: code-review",
        "    keywords: [code review]",
        "    pipeline: code-review",
    )
    return IdentityCatalog.load(tmp_path)


class _FixedLLM:
    """Returns a canned route token for every routing query."""

    def __init__(self, token: str = "coding:code-review") -> None:
        self.token = token
        self.calls = 0

    async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
        self.calls += 1
        return ChatResponse(text=self.token)


class _RaisingLLM:
    async def chat(self, messages: list[dict], **_: object) -> ChatResponse:
        raise RuntimeError("LLM routing backend unavailable")


def test_keyword_hit_never_consults_the_llm(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    llm = _FixedLLM(token="coding:code-review")

    decision = asyncio.run(route_task_with_llm("帮我做需求调研", catalog, llm=llm))

    assert decision.identity_id == "coding"
    assert decision.route_id == "requirements-research"
    assert decision.pipeline_id == "requirements-research"
    assert llm.calls == 0


def test_llm_selects_a_declared_route_when_keywords_miss(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    llm = _FixedLLM(token="coding:code-review")

    decision = asyncio.run(route_task_with_llm("帮忙看看这段代码有没有问题", catalog, llm=llm))

    assert decision.identity_id == "coding"
    assert decision.route_id == "code-review"
    assert decision.pipeline_id == "code-review"
    assert llm.calls == 1


def test_llm_direct_answer_keeps_the_deterministic_fallback(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    llm = _FixedLLM(token="DIRECT")

    decision = asyncio.run(route_task_with_llm("帮忙看看这段代码有没有问题", catalog, llm=llm))

    assert decision.identity_id == "smith"
    assert decision.route_id == "direct"
    assert decision.pipeline_id is None


def test_llm_failure_falls_back_to_deterministic_routing(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    llm = _RaisingLLM()

    decision = asyncio.run(route_task_with_llm("帮忙看看这段代码有没有问题", catalog, llm=llm))

    assert decision.identity_id == "smith"
    assert decision.route_id == "direct"
    assert decision.pipeline_id is None


def test_llm_inventing_an_undeclared_route_falls_back(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    llm = _FixedLLM(token="coding:does-not-exist")

    decision = asyncio.run(route_task_with_llm("帮忙看看这段代码有没有问题", catalog, llm=llm))

    assert decision.identity_id == "smith"
    assert decision.route_id == "direct"
    assert decision.pipeline_id is None
