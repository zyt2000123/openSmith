"""Behavior regressions discovered during the harness review."""
from __future__ import annotations

import asyncio
from pathlib import Path

from engine.execution.react.react_loop import react_event_loop
from engine.llm.contracts import ChatResponse, ToolCallData
from engine.tool.registry import ToolRegistry
from engine.tool import truncation


def test_archived_outputs_remain_distinct_within_one_second(tmp_path, monkeypatch):
    monkeypatch.setattr(truncation, "_OUTPUT_DIR", tmp_path)
    first = truncation.truncate_output("same\n" * 3000, tool_name="read_file")
    second = truncation.truncate_output("same\n" * 2000 + "different\n" * 1000,
                                        tool_name="read_file")
    def ref(preview):
        return Path(preview.split("Full output saved to: ", 1)[1].splitlines()[0])
    first_path, second_path = ref(first), ref(second)
    assert first_path != second_path, "two calls overwrite the same archived output"
    assert first_path.read_text() == "same\n" * 3000
    assert second_path.read_text().endswith("different\n" * 1000)


def test_changing_poll_results_do_not_trigger_stagnation_warning():
    class PollModel:
        def __init__(self):
            self.requests = []

        async def chat(self, messages, tools=None, prefix_cache_key=None):
            self.requests.append([dict(m) for m in messages])
            n = len(self.requests)
            if n <= 4:
                return ChatResponse(tool_calls=[
                    ToolCallData(id=f"poll-{n}", name="poll", arguments={})
                ])
            return ChatResponse(text="The job is complete.")

    async def run():
        model = PollModel()
        tools = ToolRegistry()
        sequence = 0

        async def poll():
            nonlocal sequence
            sequence += 1
            return f"completed_items={sequence}"

        tools.register("poll", "Read job progress", {}, poll)
        events = [e async for e in react_event_loop(
            model, [{"role": "user", "content": "Track the job progress"}],
            tools, max_iters=8,
        )]
        return model, events

    model, _ = asyncio.run(run())
    assert not any("identical arguments" in str(m.get("content", ""))
                   for m in model.requests[-1]), "progressing results still trigger repeat warning"
