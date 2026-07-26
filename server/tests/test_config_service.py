from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.routers.config import LLMConfig, get_config_service, router as config_router  # noqa: E402
from app.services import config_service as config_service_module  # noqa: E402
from app.services.config_service import ConfigService  # noqa: E402
from common.yaml_utils import load_yaml  # noqa: E402
from engine.llm import model_config  # noqa: E402


def test_config_service_persists_routes_and_timeout_profiles_without_exposing_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: primary-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)
    service = ConfigService()

    saved = service.set_llm_config(
        updates={
            "routes": {
                "gate": {
                    "model": "cheap-gate-model",
                    "api_key": "gate-secret",
                    "timeout_profile": "gate",
                },
            },
            "timeout_profiles": {"gate": {"read": 45, "stream_read": 50}},
        },
    )

    assert saved["configured"] is True
    assert saved["has_api_key"] is True
    assert "api_key" not in saved
    assert saved["routes"]["gate"] == {
        "model": "cheap-gate-model",
        "timeout_profile": "gate",
        "has_api_key": True,
    }
    assert saved["timeout_profiles"] == {"gate": {"read": 45.0, "stream_read": 50.0}}

    service.set_llm_config(updates={"routes": {"gate": {"model": "cheaper-gate-model"}}})
    stored = load_yaml(config_path)
    assert stored["llm"]["api_key"] == "primary-secret"
    assert stored["llm"]["routes"]["gate"] == {
        "model": "cheaper-gate-model",
        "api_key": "gate-secret",
        "timeout_profile": "gate",
    }


def test_config_service_can_remove_route_overrides_and_profile_fields(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: primary-model
  routes:
    gate:
      model: cheap-gate-model
  timeout_profiles:
    gate:
      read: 45
      stream_read: 50
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(
        updates={
            "routes": {"gate": None},
            "timeout_profiles": {"gate": {"read": None}},
        },
    )

    assert saved["routes"] == {}
    assert saved["timeout_profiles"] == {"gate": {"stream_read": 50.0}}
    stored = load_yaml(config_path)
    assert "routes" not in stored["llm"]
    assert stored["llm"]["timeout_profiles"] == {"gate": {"stream_read": 50.0}}


def test_config_service_persists_context_windows_per_route_and_model(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(
        updates={
            "context_window": 200_000,
            "routes": {"gate": {"context_window": 128_000}},
            "models": {"large-relay": {"context_window": 1_000_000}},
        },
    )

    assert saved["context_window"] == 200_000
    assert saved["routes"]["gate"]["context_window"] == 128_000
    assert saved["models"]["large-relay"]["context_window"] == 1_000_000
    stored = load_yaml(config_path)
    assert stored["llm"]["context_window"] == 200_000
    assert stored["llm"]["routes"]["gate"]["context_window"] == 128_000
    assert stored["llm"]["models"]["large-relay"]["context_window"] == 1_000_000


def test_config_api_accepts_and_returns_nested_configuration(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)
    app = FastAPI()
    app.include_router(config_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/config/llm",
            json={
                "vendor": "Example Relay",
                "provider": "openai",
                "api_key": "primary-secret",
                "base_url": "https://primary.example/v1",
                "model": "primary-model",
                "routes": {
                    "background": {"model": "cheap-background-model", "timeout_profile": "background"},
                },
                "timeout_profiles": {"background": {"read": 240, "stream_read": 300}},
            },
        )

        assert response.status_code == 200
        assert response.json()["vendor"] == "Example Relay"
        assert response.json()["routes"]["background"] == {
            "model": "cheap-background-model",
            "timeout_profile": "background",
            "has_api_key": False,
        }
        assert response.json()["timeout_profiles"] == {"background": {"read": 240.0, "stream_read": 300.0}}
        assert "api_key" not in response.json()
        assert client.get("/api/config/llm").json()["configured"] is True


@pytest.mark.parametrize(
    "base_url",
    ["http://relay.example/v1", "https://127.0.0.1/v1", "https://localhost/v1"],
)
def test_config_api_rejects_insecure_or_private_relay_endpoints(monkeypatch, tmp_path: Path, base_url: str) -> None:
    monkeypatch.setattr(ConfigService, "_config_path", tmp_path / "config.yaml")
    app = FastAPI()
    app.include_router(config_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/config/llm",
            json={"provider": "openai", "api_key": "test-key", "base_url": base_url, "model": "test-model"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_model_discovery_rejects_a_persisted_local_relay_before_sending_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: relay-secret
  base_url: https://localhost/v1
  model: relay-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    with pytest.raises(HTTPException, match="private or local") as exc_info:
        await ConfigService().list_relay_models()

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_config_service_discovers_models_from_the_interactive_relay_without_returning_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: base-secret
  base_url: https://base.example/v1
  model: base-model
  routes:
    interactive:
      api_key: relay-secret
      base_url: https://relay.example/v1
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    calls: list[tuple[str, dict[str, str]]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return {"object": "list", "data": [{"id": "GLM-5.2"}, {"id": "gpt-4.1"}, {"id": "GLM-5.2"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            calls.append((url, headers))
            return FakeResponse()

    monkeypatch.setattr(
        config_service_module,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **_kwargs: FakeClient()),
        raising=False,
    )

    result = await ConfigService().list_relay_models()

    assert result == {"models": ["GLM-5.2", "gpt-4.1"]}
    assert calls == [("https://relay.example/v1/models", {"Authorization": "Bearer relay-secret"})]
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_config_service_rejects_model_discovery_for_native_anthropic(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: anthropic
  api_key: anthropic-secret
  base_url: https://api.anthropic.com
  model: claude-test
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    with pytest.raises(HTTPException, match="OpenAI-compatible"):
        await ConfigService().list_relay_models()


def test_config_api_lists_discovered_models_without_exposing_credentials() -> None:
    class FakeConfigService:
        async def list_relay_models(self) -> dict[str, list[str]]:
            return {"models": ["GLM-5.2"]}

    app = FastAPI()
    app.include_router(config_router)
    app.dependency_overrides[get_config_service] = FakeConfigService

    with TestClient(app) as client:
        response = client.get("/api/config/llm/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["GLM-5.2"]}


@pytest.mark.asyncio
async def test_config_service_rejects_an_invalid_relay_model_list(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: relay-secret
  base_url: https://relay.example/v1
  model: default-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return {"models": ["GLM-5.2"]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, *, headers: dict[str, str]) -> FakeResponse:
            assert headers == {"Authorization": "Bearer relay-secret"}
            return FakeResponse()

    monkeypatch.setattr(
        config_service_module,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **_kwargs: FakeClient()),
    )

    with pytest.raises(HTTPException, match="invalid model list") as exc_info:
        await ConfigService().list_relay_models()

    assert exc_info.value.status_code == 502


def test_persisted_api_configuration_resolves_to_engine_route(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_path = data_dir / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)
    ConfigService().set_llm_config(
        updates={
            "api_key": "primary-secret",
            "base_url": "https://primary.example/v1",
            "model": "primary-model",
            "routes": {"gate": {"model": "cheap-gate-model", "timeout_profile": "gate"}},
            "timeout_profiles": {"gate": {"read": 45, "stream_read": 50}},
        },
    )
    monkeypatch.setattr(model_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(model_config, "SMITH_PROFILE_DIR", tmp_path / "missing-smith")
    monkeypatch.setattr(model_config, "AGENT_DIR", tmp_path / "missing-agent")
    for name in (
        "AGENTSMITH_LLM_API_KEY",
        "AGENTSMITH_LLM_BASE_URL",
        "AGENTSMITH_LLM_MODEL",
        "AGENTSMITH_LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)

    gate = model_config.resolve_llm_config(usage=model_config.LLMUsage.GATE)

    assert gate["api_key"] == "primary-secret"
    assert gate["model"] == "cheap-gate-model"
    assert gate["timeout"] == {
        "connect": 10.0,
        "read": 45.0,
        "stream_read": 50.0,
        "write": 30.0,
        "pool": 10.0,
    }


@pytest.mark.parametrize("usage", ["not-a-route", "vision"])
def test_config_route_rejects_unknown_route_names(usage: str) -> None:
    with pytest.raises(ValidationError):
        LLMConfig(routes={usage: {"model": "unexpected"}})


def test_config_route_rejects_boolean_timeout_values() -> None:
    with pytest.raises(ValidationError):
        LLMConfig(timeout_profiles={"gate": {"read": True}})


def test_config_service_ignores_legacy_vision_config_and_cleans_it_on_save(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A removed visual route must not brick reads or survive the next save."""
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)
    config_path.write_text(
        """
llm:
  api_key: primary-secret
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

    service = ConfigService()
    public = service.get_llm_config()

    assert public["routes"] == {"gate": {"model": "review-model", "has_api_key": False}}
    assert public["timeout_profiles"] == {}

    service.set_llm_config(updates={"model": "updated-primary-model"})
    stored = load_yaml(config_path)
    assert "vision" not in stored["llm"].get("routes", {})
    assert "vision" not in stored["llm"].get("timeout_profiles", {})
    assert stored["llm"]["routes"]["gate"] == {"model": "review-model"}
    assert config_service_module._USAGES == {usage.value for usage in model_config.LLMUsage}


def test_config_service_persists_native_provider_route_and_generation_limit(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(
        updates={
            "provider": "openai",
            "api_key": "primary-secret",
            "base_url": "https://primary.example/v1",
            "model": "primary-model",
            "max_output_tokens": 2048,
            "routes": {
                "gate": {
                    "provider": "anthropic",
                    "api_key": "anthropic-secret",
                    "base_url": "https://api.anthropic.com",
                    "model": "claude-test",
                    "max_output_tokens": 512,
                },
            },
        },
    )

    assert saved["max_output_tokens"] == 2048
    assert saved["routes"]["gate"] == {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-test",
        "max_output_tokens": 512,
        "has_api_key": True,
    }
    stored = load_yaml(config_path)
    assert stored["llm"]["routes"]["gate"]["api_key"] == "anthropic-secret"


def test_config_service_does_not_invent_a_generation_limit(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: primary-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    assert ConfigService().get_llm_config()["max_output_tokens"] is None


def test_config_service_persists_named_model_profiles_without_exposing_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(
        updates={
            "models": {
                "relay-fast": {
                    "provider": "openai",
                    "api_key": "relay-secret",
                    "base_url": "https://relay.example/v1",
                    "model": "fast-model",
                    "max_output_tokens": 1024,
                },
            },
        },
    )

    assert saved["models"] == {
        "relay-fast": {
            "provider": "openai",
            "base_url": "https://relay.example/v1",
            "model": "fast-model",
            "max_output_tokens": 1024,
            "has_api_key": True,
        },
    }
    assert load_yaml(config_path)["llm"]["models"]["relay-fast"]["api_key"] == "relay-secret"


def test_config_api_rejects_invalid_named_model_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ConfigService, "_config_path", tmp_path / "config.yaml")
    app = FastAPI()
    app.include_router(config_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/config/llm",
            json={"models": {"relay-fast": {"model": ""}}},
        )

    assert response.status_code == 422


def test_config_service_reports_the_interactive_route_as_the_effective_llm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A route override is what actually runs, so the UI has to be shown that."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: base-model
  routes:
    interactive:
      model: route-model
      base_url: https://route.example/v1
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    shown = ConfigService().get_llm_config()

    assert shown["model"] == "route-model"
    assert shown["base_url"] == "https://route.example/v1"
    assert shown["provider"] == "openai"


def test_config_service_lets_a_model_patch_clear_a_shadowing_interactive_route(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Editing the shown model must take effect, not lose to a stale route copy."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: base-model
  routes:
    interactive:
      model: stale-model
      timeout_profile: interactive
    gate:
      model: gate-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(updates={"model": "chosen-model"})

    assert saved["model"] == "chosen-model"
    stored = load_yaml(config_path)
    assert stored["llm"]["model"] == "chosen-model"
    assert stored["llm"]["routes"]["interactive"] == {"timeout_profile": "interactive"}
    assert stored["llm"]["routes"]["gate"] == {"model": "gate-model"}


def test_config_service_keeps_an_explicitly_patched_interactive_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Naming the route model in the same patch is intent, not a stale leftover."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai
  api_key: primary-secret
  base_url: https://primary.example/v1
  model: base-model
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigService, "_config_path", config_path)

    saved = ConfigService().set_llm_config(
        updates={"model": "base-choice", "routes": {"interactive": {"model": "route-choice"}}},
    )

    assert saved["model"] == "route-choice"
    stored = load_yaml(config_path)
    assert stored["llm"]["model"] == "base-choice"
    assert stored["llm"]["routes"]["interactive"]["model"] == "route-choice"


def test_config_api_rejects_unknown_provider_and_boolean_generation_limit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ConfigService, "_config_path", tmp_path / "config.yaml")
    app = FastAPI()
    app.include_router(config_router)

    with TestClient(app) as client:
        unknown_provider = client.post("/api/config/llm", json={"provider": "unknown-provider"})
        assert unknown_provider.status_code == 422

    with pytest.raises(ValidationError):
        LLMConfig(max_output_tokens=True)
