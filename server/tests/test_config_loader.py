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

    assert catalog.resolve("修复登录报错").pipeline_id == "coding"
    assert catalog.resolve("新增导出功能").pipeline_id == "coding"


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

    def fake_resolve(*, usage: model_config.LLMUsage) -> dict:
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
    monkeypatch.setattr(engine_runtime, "ToolRegistry", lambda: object())
    monkeypatch.setattr(engine_runtime, "SkillRegistry", lambda: object())
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
        model_config.LLMUsage.VISION,
    ]
    assert services.llm is clients[0]
    assert services.gate_llm is clients[1]
    assert services.background_llm is clients[2]
    # 这里的 fake 给每个 usage 不同的 model，等价于"配了独立图片模型"。
    assert services.vision_llm is clients[3]
    assert services.owns_llm_clients is False


def test_build_engine_runtime_skips_the_image_client_when_no_image_model_is_set(monkeypatch) -> None:
    """未配 image model 时 vision 路由继承主模型（resolve_llm_config 的既有行为）。

    此时必须是 None：白建一个客户端只会多占一条 httpx 连接，更糟的是
    engine 会以为存在一个可回落的图片模型，而它其实和读不了图的主模型是同一个。
    """
    clients: list[object] = []

    def fake_resolve(*, usage: model_config.LLMUsage) -> dict:
        return {
            "usage": usage.value,
            "provider": "openai",
            "model": "one-model-for-everything",
            "base_url": "https://provider.example/v1",
        }

    monkeypatch.setattr(engine_runtime, "resolve_llm_config", fake_resolve)
    monkeypatch.setattr(engine_runtime, "build_llm_client", lambda config: clients.append(config) or object())
    monkeypatch.setattr(engine_runtime, "load_runtime_identity_catalog", lambda: object())
    monkeypatch.setattr(engine_runtime, "ToolRegistry", lambda: object())
    monkeypatch.setattr(engine_runtime, "SkillRegistry", lambda: object())
    monkeypatch.setattr(engine_runtime, "ToolGuard", lambda _path: object())

    _, services = engine_runtime.build_engine_runtime("smith-id", "Smith")

    assert services.vision_llm is None


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


def test_llm_client_manager_normalizes_gemini_default_endpoint(monkeypatch) -> None:
    clients: list[object] = []

    def fake_build(config: dict) -> object:
        client = object()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_runtime, "build_llm_client", fake_build)
    manager = engine_runtime.LLMClientManager()
    base = {
        "provider": "gemini",
        "api_key": "key",
        "model": "gemini-3.5-flash",
    }

    first = manager.get_for_config(dict(base, base_url=""))
    second = manager.get_for_config(dict(
        base,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    ))

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


def test_route_can_select_gemini_adapter_with_default_endpoint(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        """
llm:
  provider: gemini
  api_key: gemini-key
  model: gemini-3.5-flash
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")

    cfg = model_config.resolve_llm_config()
    client = model_config.build_llm_client(cfg)
    try:
        assert cfg["provider"] == "gemini"
        assert client.provider == "gemini"
        assert type(client.adapter).__name__ == "GeminiAdapter"
        assert client.adapter.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    finally:
        import asyncio
        asyncio.run(client.close())
