"""The cost-tracker hook must actually load, and must not invent prices.

Two independent defects made it inert.  It imported ``get_data_root`` from
``common.paths`` — a name that exists nowhere in the repo — so the loader hit
``ImportError``, logged it, and returned None, leaving the stop-hook registry
empty while ``hooks.yaml`` declared ``enabled: true``.  Separately, unmatched
models fell back to Claude Sonnet pricing, which would have stamped a
plausible dollar figure onto gpt/gemini/local runs once loading was fixed.

No test loaded the real ``agents/smith/hooks.yaml`` and asserted the registry
was non-empty; a single assertion would have caught the dead import.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_cost_tracker():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agents.smith.hooks.cost_tracker")


def test_module_imports() -> None:
    """The dead import made every other behaviour unreachable."""
    module = _load_cost_tracker()

    assert hasattr(module, "CostTrackerHook")


def test_known_model_is_priced() -> None:
    hook = _load_cost_tracker().CostTrackerHook()

    assert hook._calculate_cost("claude-sonnet-4-20250101", 1_000_000, 1_000_000) == 18.0


def test_unknown_model_is_not_guessed() -> None:
    """A relayed gpt/gemini/local run must not be billed at Sonnet rates."""
    hook = _load_cost_tracker().CostTrackerHook()

    assert hook._calculate_cost("gpt-5", 1_000_000, 1_000_000) is None
    assert hook._calculate_cost("unknown", 1_000_000, 1_000_000) is None


def test_record_is_written_with_private_permissions(tmp_path, monkeypatch) -> None:
    module = _load_cost_tracker()
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    hook = module.CostTrackerHook()

    asyncio.run(
        hook.run(
            "sess-1",
            {
                "session_stats": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "model": "claude-sonnet-4",
                }
            },
        )
    )

    cost_file = tmp_path / "metrics" / "costs.jsonl"
    assert cost_file.is_file(), "no cost record was written"
    record = json.loads(cost_file.read_text().splitlines()[0])
    assert record["session_id"] == "sess-1"
    assert record["total_tokens"] == 1500
    assert record["estimated_cost_usd"] == pytest.approx(0.0105)
    assert cost_file.stat().st_mode & 0o777 == 0o600


def test_unknown_model_record_carries_no_invented_cost(tmp_path, monkeypatch) -> None:
    """A null cost is honest; round(None) would have crashed the hook."""
    module = _load_cost_tracker()
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    hook = module.CostTrackerHook()

    asyncio.run(
        hook.run(
            "sess-2",
            {"session_stats": {"input_tokens": 10, "output_tokens": 5, "model": "gpt-5"}},
        )
    )

    record = json.loads((tmp_path / "metrics" / "costs.jsonl").read_text().splitlines()[0])
    assert record["estimated_cost_usd"] is None
    assert record["total_tokens"] == 15
