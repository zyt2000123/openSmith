"""Tests for the local llm.pricing table resolution."""

from __future__ import annotations

import pytest

import engine.llm.model_config as model_config
from engine.llm.model_config import resolve_price_table


def test_price_table_parses_and_filters_invalid_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "llm": {
            "pricing": {
                "deepseek-v4-pro": {
                    "input": 0.28,
                    "output": 1.14,
                    "cache_read": 0.028,
                    "bogus_key": 9.9,
                    "cache_write": -1,
                },
                "broken-model": "junk",
                "bool-priced": {"input": True},
            },
        },
    }
    monkeypatch.setattr(model_config, "load_yaml", lambda _path: config)

    table = resolve_price_table()

    assert table == {
        "deepseek-v4-pro": {
            "input": 0.28,
            "output": 1.14,
            "cache_read": 0.028,
        },
    }


def test_price_table_empty_when_pricing_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_config, "load_yaml", lambda _path: {"llm": {"model": "m"}})
    assert resolve_price_table() == {}

    monkeypatch.setattr(model_config, "load_yaml", lambda _path: {})
    assert resolve_price_table() == {}
