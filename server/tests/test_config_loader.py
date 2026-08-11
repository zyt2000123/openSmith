from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.yaml_utils import YamlConfigError  # noqa: E402
from app.services import engine_runtime  # noqa: E402
from engine.llm import model_config  # noqa: E402


def test_resolve_llm_config_loads_builtin_smith_profile_by_default(
    tmp_path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    smith_dir = tmp_path / "smith"
    smith_dir.mkdir()
    (smith_dir / "config.yaml").write_text(
        "llm:\n  model: smith-default\n  provider: openai\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", smith_dir)
    monkeypatch.setattr(model_config, "AGENT_DIR", data_dir / "agent")
    for env_key in (
        "AGENTSMITH_LLM_API_KEY",
        "AGENTSMITH_LLM_BASE_URL",
        "AGENTSMITH_LLM_MODEL",
        "AGENTSMITH_LLM_PROVIDER",
    ):
        monkeypatch.delenv(env_key, raising=False)

    cfg = model_config.resolve_llm_config()
    gate_cfg = model_config.resolve_llm_config(
        usage=model_config.LLMUsage.GATE,
    )

    assert cfg["model"] == "smith-default"
    assert cfg["provider"] == "openai"
    assert gate_cfg["model"] == "smith-default"  # 未配置 route 时回退主模型
    assert gate_cfg["timeout"]["read"] == 90.0
    assert gate_cfg["timeout"]["stream_read"] == 90.0


def test_runtime_catalog_validates_the_shipped_coding_pipeline() -> None:
    catalog = engine_runtime.load_runtime_identity_catalog(force=True)

    assert catalog.resolve("请做需求调研，给我方案").pipeline_id == "requirements-research"
    assert catalog.resolve("请用 TDD 修复登录报错").pipeline_id == "tdd-development"
    assert catalog.resolve("请做代码评审").pipeline_id == "code-review"
    assert catalog.resolve("修复登录报错").pipeline_id is None


def test_resolve_llm_config_selects_model_routes_and_timeout_profiles(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  api_key: primary-key
  base_url: https://primary.example/v1
  model: primary-model
  context_window: 200000
  stream: true
  routes:
    gate:
      model: cheap-gate-model
      context_window: 128000
    background:
      base_url: https://background.example/v1
      model: cheap-background-model
  timeout_profiles:
    gate:
      read: 45
    background:
      read: 250
      stream_read: 280
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    interactive = model_config.resolve_llm_config(
        usage=model_config.LLMUsage.INTERACTIVE,
    )
    gate = model_config.resolve_llm_config(usage=model_config.LLMUsage.GATE)
    background = model_config.resolve_llm_config(
        usage=model_config.LLMUsage.BACKGROUND,
    )

    assert interactive["model"] == "primary-model"
    assert interactive["context_window"] == 200000
    assert interactive["timeout"]["read"] == 90.0
    assert interactive["timeout"]["stream_read"] == 120.0
    assert gate["api_key"] == "primary-key"
    assert gate["model"] == "cheap-gate-model"
    assert gate["context_window"] == 128000
    assert gate["timeout"]["read"] == 45.0
    assert gate["timeout"]["stream_read"] == 90.0
    assert background["base_url"] == "https://background.example/v1"
    assert background["model"] == "cheap-background-model"
    assert background["context_window"] == 200000
    assert background["timeout"]["read"] == 250.0
    assert background["timeout"]["stream_read"] == 280.0


def test_resolve_llm_config_ignores_legacy_vision_timeout_profile(tmp_path, monkeypatch) -> None:
    """Old visual-route settings must not break direct engine startup."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  api_key: primary-key
  base_url: https://primary.example/v1
  model: primary-model
  routes:
    vision:
      model: image-model
      api_key: image-secret
    gate:
      model: review-model
      timeout_profile: vision
  timeout_profiles:
    vision:
      read: 90
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")
    for env_key in (
        "AGENTSMITH_LLM_API_KEY",
        "AGENTSMITH_LLM_BASE_URL",
        "AGENTSMITH_LLM_MODEL",
        "AGENTSMITH_LLM_PROVIDER",
    ):
        monkeypatch.delenv(env_key, raising=False)

    gate = model_config.resolve_llm_config(usage=model_config.LLMUsage.GATE)

    assert gate["model"] == "review-model"
    assert gate["timeout"] == {
        "connect": 10.0,
        "read": 90.0,
        "stream_read": 90.0,
        "write": 30.0,
        "pool": 10.0,
    }


def test_resolve_llm_config_preserves_vendor_only_as_display_metadata(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  vendor: Example Relay
  provider: openai
  api_key: key
  base_url: https://relay.example/v1
  model: model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    resolved = model_config.resolve_llm_config()

    assert resolved["vendor"] == "Example Relay"


def test_resolve_llm_config_rejects_a_non_mapping_llm_config(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text("llm: invalid\n", encoding="utf-8")

    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    with pytest.raises(YamlConfigError, match="LLM configuration"):
        model_config.resolve_llm_config()


@pytest.mark.parametrize("field", ["api_key", "base_url", "model"])
def test_build_llm_client_fails_fast_for_missing_required_config(field: str) -> None:
    config = {
        "api_key": "test-key",
        "base_url": "https://provider.example/v1",
        "model": "test-model",
    }
    config[field] = ""

    with pytest.raises(YamlConfigError, match=field):
        model_config.build_llm_client(config)


def test_resolve_llm_config_rejects_non_finite_timeout_values(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  api_key: test-key
  base_url: https://provider.example/v1
  model: test-model
  timeout_profiles:
    gate:
      read: .inf
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    with pytest.raises(YamlConfigError, match="positive number"):
        model_config.resolve_llm_config(usage=model_config.LLMUsage.GATE)


def test_resolve_llm_config_selects_named_model_profile(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  api_key: base-key
  base_url: https://base.example/v1
  model: base-model
  models:
    relay-fast:
      provider: anthropic
      api_key: relay-key
      base_url: https://relay.example/v1
      model: fast-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    selected = model_config.resolve_llm_config(model_profile="relay-fast")

    assert selected["provider"] == "anthropic"
    assert selected["model"] == "fast-model"
    assert selected["api_key"] == "relay-key"


def test_resolve_llm_config_profile_can_reuse_the_default_relay(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: openai
  api_key: relay-key
  base_url: https://relay.example/v1
  model: default-model
  models:
    glm-5-2:
      model: GLM-5.2
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    selected = model_config.resolve_llm_config(model_profile="glm-5-2")

    assert selected["provider"] == "openai"
    assert selected["api_key"] == "relay-key"
    assert selected["base_url"] == "https://relay.example/v1"
    assert selected["model"] == "GLM-5.2"


def test_build_engine_runtime_selects_interactive_gate_and_background_clients(monkeypatch) -> None:
    selected_usages: list[model_config.LLMUsage] = []
    clients: list[object] = []

    class FakeSkillRegistry:
        def load_builtin(self, _root: Path) -> None:
            pass

    def fake_resolve(
        *,
        usage: model_config.LLMUsage,
    ) -> dict:
        selected_usages.append(usage)
        return {
            "usage": usage.value,
            "vendor": "Example Relay",
            "provider": "openai",
            "model": f"{usage.value}-model",
            "api_key": "must-not-reach-the-prompt",
            "base_url": "https://provider.example/v1",
        }

    def fake_build(config: dict) -> object:
        client = object()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_runtime, "resolve_llm_config", fake_resolve)
    monkeypatch.setattr(engine_runtime, "build_llm_client", fake_build)
    monkeypatch.setattr(engine_runtime, "load_runtime_identity_catalog", lambda: object())
    monkeypatch.setattr(engine_runtime, "ToolRegistry", lambda **_: object())
    monkeypatch.setattr(engine_runtime, "SkillRegistry", FakeSkillRegistry)
    monkeypatch.setattr(engine_runtime, "ToolGuard", lambda _path: object())

    runtime, services = engine_runtime.build_engine_runtime("smith-id", "Smith")

    assert runtime.agent_id == "smith-id"
    assert runtime.metadata == {
        "current_vendor": "Example Relay",
        "current_provider": "openai",
        "current_model": "interactive-model",
    }
    assert selected_usages == [
        model_config.LLMUsage.INTERACTIVE,
        model_config.LLMUsage.GATE,
        model_config.LLMUsage.BACKGROUND,
    ]
    assert services.llm is clients[0]
    assert services.gate_llm is clients[1]
    assert services.background_llm is clients[2]
    assert services.owns_llm_clients is False


def test_llm_client_manager_reuses_clients_for_identical_config(monkeypatch) -> None:
    clients: list[object] = []

    def fake_build(config: dict) -> object:
        client = object()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_runtime, "build_llm_client", fake_build)
    manager = engine_runtime.LLMClientManager()
    config = {
        "provider": "openai",
        "api_key": "key",
        "base_url": "https://provider.example/v1",
        "model": "model",
        "stream": True,
        "timeout": {"read": 90.0},
    }

    first = manager.get_for_config(dict(config))
    second = manager.get_for_config(dict(config, vendor="Example Relay"))

    assert first is second
    assert clients == [first]


def test_llm_client_manager_closes_unique_cached_clients_once(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    clients: list[FakeClient] = []

    def fake_build(config: dict) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_runtime, "build_llm_client", fake_build)
    manager = engine_runtime.LLMClientManager()
    shared = {
        "provider": "openai",
        "api_key": "key",
        "base_url": "https://provider.example/v1",
        "model": "model",
    }
    other = dict(shared, model="other-model")

    manager.get_for_config(dict(shared))
    manager.get_for_config(dict(shared))
    manager.get_for_config(other)

    import asyncio
    asyncio.run(manager.close())

    assert [client.closed for client in clients] == [1, 1]


def test_route_can_select_native_anthropic_adapter_and_generation_limit(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: openai
  api_key: openai-key
  base_url: https://openai.example/v1
  model: openai-model
  routes:
    gate:
      provider: anthropic
      api_key: anthropic-key
      base_url: https://api.anthropic.com
      model: claude-test
      max_output_tokens: 768
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    gate = model_config.resolve_llm_config(usage=model_config.LLMUsage.GATE)
    client = model_config.build_llm_client(gate)
    try:
        assert gate["provider"] == "anthropic"
        assert gate["max_output_tokens"] == 768
        assert client.provider == "anthropic"
        assert type(client.adapter).__name__ == "AnthropicAdapter"
    finally:
        import asyncio
        asyncio.run(client.close())


def test_route_can_select_anthropic_adapter(tmp_path, monkeypatch) -> None:
    """A config.yaml naming the Anthropic protocol must build its native adapter."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: anthropic
  api_key: anthropic-key
  base_url: https://api.anthropic.com
  model: claude-sonnet-4-20250514
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    cfg = model_config.resolve_llm_config()
    client = model_config.build_llm_client(cfg)
    try:
        assert cfg["provider"] == "anthropic"
        assert client.provider == "anthropic"
        assert type(client.adapter).__name__ == "AnthropicAdapter"
        assert client.adapter.base_url == "https://api.anthropic.com"
    finally:
        import asyncio
        asyncio.run(client.close())


def test_http_schema_matches_the_canonical_field_table() -> None:
    """The HTTP models are the one projection that cannot be derived.

    They are written out by hand for their validators and generated docs, so
    guard them instead: a field added to the canonical table but not to the
    schema would be rejected with a 422 that names no cause, and a field added
    to the schema alone would be dropped before it reached the engine.
    """
    from app.routers.config import LLMConfig, LLMRoutePatch
    from engine.llm.config_fields import BASE_ONLY_FIELDS, ROUTE_FIELDS

    assert set(LLMRoutePatch.model_fields) == set(ROUTE_FIELDS)

    # A route picks a shared timeout profile, so `timeout_profile` is
    # route-scoped only; the top level instead carries the profile definitions
    # and the nested route/model collections.
    assert set(LLMConfig.model_fields) == (
        (set(ROUTE_FIELDS) - {"timeout_profile"})
        | set(BASE_ONLY_FIELDS)
        | {"routes", "models", "timeout_profiles"}
    )


def test_route_level_thinking_reaches_the_anthropic_adapter(tmp_path, monkeypatch) -> None:
    """`thinking` must merge per route like every other route-scoped field."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: anthropic
  api_key: anthropic-key
  base_url: https://api.anthropic.com
  model: claude-opus-4-8
  routes:
    interactive:
      thinking: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    import asyncio

    interactive = model_config.build_llm_client(
        model_config.resolve_llm_config(usage="interactive")
    )
    background = model_config.build_llm_client(
        model_config.resolve_llm_config(usage="background")
    )
    try:
        assert interactive.adapter.thinking is True
        # Routes that did not opt in keep the safe default.
        assert background.adapter.thinking is False
    finally:
        asyncio.run(interactive.close())
        asyncio.run(background.close())


def test_client_build_rejects_removed_gemini_protocol(tmp_path, monkeypatch) -> None:
    """Only the two natively implemented protocols may produce a client.

    ``resolve_llm_config`` is deliberately lenient — it reports whatever is
    configured so the UI can render a half-finished setup — so the protocol
    allowlist is enforced where a client is actually built.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: gemini
  api_key: some-key
  model: some-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    cfg = model_config.resolve_llm_config()
    assert cfg["provider"] == "gemini"
    with pytest.raises(YamlConfigError, match="Unsupported LLM provider"):
        model_config.build_llm_client(cfg)
