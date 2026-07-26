"""Tests for provider usage normalization."""

from __future__ import annotations

from engine.llm.usage import normalize_usage


def test_openai_style_fields_map_to_all_six_keys() -> None:
    usage = normalize_usage({
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 140,
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {"reasoning_tokens": 12},
    })
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "cache_read_tokens": 64,
        "cache_write_tokens": 0,
        "reasoning_tokens": 12,
    }


def test_deepseek_cache_hit_maps_to_cache_read_and_miss_is_ignored() -> None:
    usage = normalize_usage({
        "prompt_tokens": 66,
        "completion_tokens": 5,
        "total_tokens": 71,
        "prompt_cache_hit_tokens": 64,
        "prompt_cache_miss_tokens": 2,
    })
    assert usage["cache_read_tokens"] == 64
    assert usage["input_tokens"] == 66
    assert "prompt_cache_miss_tokens" not in usage


def test_anthropic_style_cache_fields() -> None:
    usage = normalize_usage({
        "input_tokens": 20,
        "output_tokens": 8,
        "cache_read_input_tokens": 512,
        "cache_creation_input_tokens": 128,
    })
    assert usage["cache_read_tokens"] == 512
    assert usage["cache_write_tokens"] == 128
    # Anthropic input_tokens excludes cached tokens; recorded as-is, no math.
    assert usage["input_tokens"] == 20
    assert usage["total_tokens"] == 28


def test_openai_detail_takes_priority_over_deepseek_alias() -> None:
    # Gateways may return both dialects; the OpenAI-style detail wins.
    usage = normalize_usage({
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 8},
        "prompt_cache_hit_tokens": 6,
    })
    assert usage["cache_read_tokens"] == 8


def test_missing_fields_default_to_zero_without_estimation() -> None:
    usage = normalize_usage({"prompt_tokens": 5, "completion_tokens": 2})
    assert usage["cache_read_tokens"] == 0
    assert usage["cache_write_tokens"] == 0
    assert usage["reasoning_tokens"] == 0
    assert usage["total_tokens"] == 7


def test_invalid_payloads_normalize_to_all_zero() -> None:
    assert normalize_usage(None) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert normalize_usage({"prompt_tokens": "junk"})["input_tokens"] == 0
    assert normalize_usage({"prompt_tokens_details": "junk"})["cache_read_tokens"] == 0


def test_negative_and_float_values_are_sanitized() -> None:
    usage = normalize_usage({
        "prompt_tokens": -5,
        "completion_tokens": 3.0,
        "completion_tokens_details": {"reasoning_tokens": -1},
    })
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 3
    assert usage["reasoning_tokens"] == 0
