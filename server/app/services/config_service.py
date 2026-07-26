from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, NoReturn

import httpx
from fastapi import HTTPException

from common.config import DATA_DIR
from common.yaml_utils import YamlConfigError, load_yaml, save_yaml
from engine.llm.factory import normalize_provider_name
from engine.llm.model_config import LLMUsage, normalize_legacy_llm_config, validate_llm_base_url
from engine.llm.contracts import UnsupportedProviderError


# Derived from the engine's own usage enum, never restated so the public
# configuration contract stays aligned with the engine's supported routes.
_USAGES = frozenset(usage.value for usage in LLMUsage)
_BASE_STRING_FIELDS = ("provider", "api_key", "base_url", "model")
_VENDOR_FIELD = "vendor"
_ROUTE_STRING_FIELDS = _BASE_STRING_FIELDS
_ROUTE_FIELDS = frozenset(
    (*_ROUTE_STRING_FIELDS, "stream", "max_output_tokens", "context_window", "timeout_profile")
)
_TIMEOUT_FIELDS = frozenset(("connect", "read", "stream_read", "write", "pool"))
_PUBLIC_ROUTE_FIELDS = (
    "provider",
    "base_url",
    "model",
    "stream",
    "max_output_tokens",
    "context_window",
    "timeout_profile",
)


class ConfigService:
    """Persist and expose the user-editable portion of LLM configuration.

    The service is intentionally the only place that understands the public
    API's patch semantics.  It keeps routers thin and makes the write-only
    handling of provider API keys consistent for the base route and every
    optional model route.
    """

    _config_path = DATA_DIR / "config.yaml"

    @staticmethod
    def _invalid(detail: str) -> NoReturn:
        raise HTTPException(status_code=422, detail=detail)

    def _mapping(self, value: object, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self._invalid(f"{label} must be a mapping")
        return value

    def _validate_usage(self, usage: object, label: str) -> str:
        if not isinstance(usage, str) or usage not in _USAGES:
            allowed = ", ".join(sorted(_USAGES))
            self._invalid(f"{label} must be one of: {allowed}")
        return usage

    def _validate_string_fields(
        self,
        values: Mapping[str, Any],
        fields: tuple[str, ...],
        label: str,
        *,
        allow_none: bool = False,
    ) -> None:
        for field in fields:
            if field not in values:
                continue
            value = values[field]
            if value is None and allow_none:
                continue
            if not isinstance(value, str) or not value.strip():
                self._invalid(f"{label}.{field} must be a non-empty string")
            if field == "provider":
                self._validate_provider(value, f"{label}.{field}")
            if field == "base_url":
                self._validate_base_url(value, f"{label}.{field}")

    def _validate_provider(self, value: object, label: str) -> None:
        try:
            normalize_provider_name(value)
        except UnsupportedProviderError as exc:
            self._invalid(f"{label}: {exc}")

    def _validate_base_url(self, value: object, label: str) -> None:
        """Restrict credential-bearing provider calls to public HTTPS endpoints."""
        try:
            validate_llm_base_url(value)
        except YamlConfigError as exc:
            self._invalid(f"{label}: {exc}")

    def _validate_max_output_tokens(self, value: object, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self._invalid(f"{label} must be a positive integer")

    def _validate_context_window(self, value: object, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self._invalid(f"{label} must be a positive integer")

    def _validate_route(self, route: Mapping[str, Any], label: str) -> None:
        unknown = set(route) - _ROUTE_FIELDS
        if unknown:
            self._invalid(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
        self._validate_string_fields(route, _ROUTE_STRING_FIELDS, label)
        if "stream" in route and not isinstance(route["stream"], bool):
            self._invalid(f"{label}.stream must be a boolean")
        if "max_output_tokens" in route:
            self._validate_max_output_tokens(route["max_output_tokens"], f"{label}.max_output_tokens")
        if "context_window" in route:
            self._validate_context_window(route["context_window"], f"{label}.context_window")
        if "timeout_profile" in route:
            self._validate_usage(route["timeout_profile"], f"{label}.timeout_profile")

    def _validate_timeout_profile(self, profile: Mapping[str, Any], label: str) -> None:
        unknown = set(profile) - _TIMEOUT_FIELDS
        if unknown:
            self._invalid(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
        for field, value in profile.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                self._invalid(f"{label}.{field} must be a positive number")

    def _validate_stored_llm(self, llm: Mapping[str, Any]) -> None:
        self._validate_string_fields(llm, (*_BASE_STRING_FIELDS, _VENDOR_FIELD), "llm", allow_none=True)
        if "stream" in llm and not isinstance(llm["stream"], bool):
            self._invalid("llm.stream must be a boolean")
        if "max_output_tokens" in llm:
            self._validate_max_output_tokens(llm["max_output_tokens"], "llm.max_output_tokens")
        if "context_window" in llm:
            self._validate_context_window(llm["context_window"], "llm.context_window")

        models = self._mapping(llm.get("models"), "llm.models")
        for name, profile in models.items():
            if not isinstance(name, str) or not name.strip():
                self._invalid("llm.models keys must be non-empty strings")
            if not isinstance(profile, dict):
                self._invalid(f"llm.models.{name} must be a mapping")
            self._validate_route(profile, f"llm.models.{name}")

        routes = self._mapping(llm.get("routes"), "llm.routes")
        for usage, route in routes.items():
            self._validate_usage(usage, "llm.routes key")
            if not isinstance(route, dict):
                self._invalid(f"llm.routes.{usage} must be a mapping")
            self._validate_route(route, f"llm.routes.{usage}")

        timeout_profiles = self._mapping(llm.get("timeout_profiles"), "llm.timeout_profiles")
        for usage, profile in timeout_profiles.items():
            self._validate_usage(usage, "llm.timeout_profiles key")
            if not isinstance(profile, dict):
                self._invalid(f"llm.timeout_profiles.{usage} must be a mapping")
            self._validate_timeout_profile(profile, f"llm.timeout_profiles.{usage}")

    def _load_config(self) -> dict[str, Any]:
        try:
            config = load_yaml(self._config_path)
        except YamlConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        llm = config.get("llm")
        if llm is not None:
            if not isinstance(llm, dict):
                self._invalid("llm configuration must be a mapping")
            llm = normalize_legacy_llm_config(llm)
            config["llm"] = llm
            self._validate_stored_llm(llm)
        return config

    @staticmethod
    def _string_or_empty(value: object) -> str:
        return value if isinstance(value, str) else ""

    def _public_routes(self, routes: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        public: dict[str, dict[str, Any]] = {}
        for usage, route in routes.items():
            if not isinstance(route, dict):
                continue  # Stored values have already been validated.
            public_route = {field: route[field] for field in _PUBLIC_ROUTE_FIELDS if field in route}
            public_route["has_api_key"] = bool(route.get("api_key"))
            public[usage] = public_route
        return public

    def _public_models(self, models: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        public: dict[str, dict[str, Any]] = {}
        for name, profile in models.items():
            if not isinstance(profile, dict):
                continue
            public_profile = {field: profile[field] for field in _PUBLIC_ROUTE_FIELDS if field in profile}
            public_profile["has_api_key"] = bool(profile.get("api_key"))
            public[name] = public_profile
        return public

    def get_llm_config(self) -> dict[str, Any]:
        cfg = self._load_config()
        llm = self._mapping(cfg.get("llm"), "llm configuration")
        routes = self._mapping(llm.get("routes"), "llm.routes")
        models = self._mapping(llm.get("models"), "llm.models")
        timeout_profiles = self._mapping(llm.get("timeout_profiles"), "llm.timeout_profiles")
        interactive = self._mapping(routes.get("interactive"), "llm.routes.interactive")

        def effective(field: str) -> object:
            return interactive[field] if field in interactive else llm.get(field)

        configured = all(
            isinstance(effective(field), str) and effective(field).strip()
            for field in ("api_key", "base_url", "model")
        )
        return {
            "configured": configured,
            "has_api_key": bool(llm.get("api_key")),
            "vendor": self._string_or_empty(llm.get("vendor")),
            # Report what the interactive route actually resolves to, not the base
            # value it may be shadowing. The setup UI edits these three fields, so
            # showing the base model made a live route override invisible.
            "provider": self._string_or_empty(effective("provider")) or "openai",
            "model": self._string_or_empty(effective("model")),
            "base_url": self._string_or_empty(effective("base_url")),
            "max_output_tokens": llm.get("max_output_tokens"),
            "context_window": llm.get("context_window"),
            "routes": self._public_routes(routes),
            "models": self._public_models(models),
            "timeout_profiles": {usage: dict(profile) for usage, profile in timeout_profiles.items()},
        }

    async def list_relay_models(self) -> dict[str, list[str]]:
        """Discover OpenAI-compatible model IDs without exposing relay credentials."""
        cfg = self._load_config()
        llm = self._mapping(cfg.get("llm"), "llm configuration")
        routes = self._mapping(llm.get("routes"), "llm.routes")
        interactive = self._mapping(routes.get("interactive"), "llm.routes.interactive")

        def effective(field: str) -> object:
            return interactive[field] if field in interactive else llm.get(field)

        provider = self._string_or_empty(effective("provider")) or "openai"
        if normalize_provider_name(provider) not in {"openai", "gemini"}:
            self._invalid("Model discovery is available only for OpenAI-compatible protocols")
        api_key = self._string_or_empty(effective("api_key")).strip()
        base_url = self._string_or_empty(effective("base_url")).strip().rstrip("/")
        if not api_key or not base_url:
            self._invalid("interactive LLM route needs an API key and base URL to list relay models")
        self._validate_base_url(base_url, "llm.routes.interactive.base_url")

        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, trust_env=False) as client:
                response = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Unable to list models from the configured relay") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Configured relay returned an invalid model list") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise HTTPException(status_code=502, detail="Configured relay returned an invalid model list")

        models = sorted(
            {
                model_id.strip()
                for item in payload["data"]
                if isinstance(item, dict)
                for model_id in [item.get("id")]
                if isinstance(model_id, str) and model_id.strip()
            },
            key=str.casefold,
        )
        return {"models": models}

    def _apply_string_patch(
        self,
        target: dict[str, Any],
        field: str,
        value: object,
        label: str,
    ) -> None:
        if value is None:
            target.pop(field, None)
            return
        if not isinstance(value, str):
            self._invalid(f"{label}.{field} must be a string")
        stripped = value.strip()
        if not stripped:
            if field == "api_key":
                return  # A blank secret is a safe no-op; null explicitly removes it.
            self._invalid(f"{label}.{field} must be a non-empty string")
        if field == "provider":
            self._validate_provider(stripped, f"{label}.{field}")
        if field == "base_url":
            self._validate_base_url(stripped, f"{label}.{field}")
        target[field] = stripped

    def _apply_route_patch(self, route: dict[str, Any], patch: Mapping[str, Any], label: str) -> None:
        unknown = set(patch) - _ROUTE_FIELDS
        if unknown:
            self._invalid(f"{label} has unknown fields: {', '.join(sorted(unknown))}")
        for field in _ROUTE_STRING_FIELDS:
            if field in patch:
                self._apply_string_patch(route, field, patch[field], label)
        if "stream" in patch:
            stream = patch["stream"]
            if stream is None:
                route.pop("stream", None)
            elif isinstance(stream, bool):
                route["stream"] = stream
            else:
                self._invalid(f"{label}.stream must be a boolean")
        if "max_output_tokens" in patch:
            max_output_tokens = patch["max_output_tokens"]
            if max_output_tokens is None:
                route.pop("max_output_tokens", None)
            else:
                self._validate_max_output_tokens(max_output_tokens, f"{label}.max_output_tokens")
                route["max_output_tokens"] = max_output_tokens
        if "context_window" in patch:
            context_window = patch["context_window"]
            if context_window is None:
                route.pop("context_window", None)
            else:
                self._validate_context_window(context_window, f"{label}.context_window")
                route["context_window"] = context_window
        if "timeout_profile" in patch:
            profile = patch["timeout_profile"]
            if profile is None:
                route.pop("timeout_profile", None)
            else:
                route["timeout_profile"] = self._validate_usage(profile, f"{label}.timeout_profile")

    def _apply_routes_patch(self, llm: dict[str, Any], value: object) -> None:
        if value is None:
            llm.pop("routes", None)
            return
        if not isinstance(value, dict):
            self._invalid("routes must be a mapping")
        if not value:
            llm.pop("routes", None)
            return

        existing = self._mapping(llm.get("routes"), "llm.routes")
        routes = {usage: dict(route) for usage, route in existing.items()}
        for usage, route_patch in value.items():
            self._validate_usage(usage, "routes key")
            if route_patch is None:
                routes.pop(usage, None)
                continue
            if not isinstance(route_patch, dict):
                self._invalid(f"routes.{usage} must be a mapping or null")
            route = routes.get(usage, {})
            self._apply_route_patch(route, route_patch, f"routes.{usage}")
            if route:
                routes[usage] = route
            else:
                routes.pop(usage, None)

        if routes:
            llm["routes"] = routes
        else:
            llm.pop("routes", None)

    def _apply_models_patch(self, llm: dict[str, Any], value: object) -> None:
        if value is None:
            llm.pop("models", None)
            return
        if not isinstance(value, dict):
            self._invalid("models must be a mapping")
        if not value:
            llm.pop("models", None)
            return

        existing = self._mapping(llm.get("models"), "llm.models")
        models = {name: dict(profile) for name, profile in existing.items()}
        for name, profile_patch in value.items():
            if not isinstance(name, str) or not name.strip():
                self._invalid("models keys must be non-empty strings")
            if profile_patch is None:
                models.pop(name, None)
                continue
            if not isinstance(profile_patch, dict):
                self._invalid(f"models.{name} must be a mapping or null")
            profile = models.get(name, {})
            self._apply_route_patch(profile, profile_patch, f"models.{name}")
            if profile:
                models[name] = profile
            else:
                models.pop(name, None)

        if models:
            llm["models"] = models
        else:
            llm.pop("models", None)

    def _apply_timeout_profiles_patch(self, llm: dict[str, Any], value: object) -> None:
        if value is None:
            llm.pop("timeout_profiles", None)
            return
        if not isinstance(value, dict):
            self._invalid("timeout_profiles must be a mapping")
        if not value:
            llm.pop("timeout_profiles", None)
            return

        existing = self._mapping(llm.get("timeout_profiles"), "llm.timeout_profiles")
        profiles = {usage: dict(profile) for usage, profile in existing.items()}
        for usage, profile_patch in value.items():
            self._validate_usage(usage, "timeout_profiles key")
            if profile_patch is None:
                profiles.pop(usage, None)
                continue
            if not isinstance(profile_patch, dict):
                self._invalid(f"timeout_profiles.{usage} must be a mapping or null")
            unknown = set(profile_patch) - _TIMEOUT_FIELDS
            if unknown:
                self._invalid(
                    f"timeout_profiles.{usage} has unknown fields: {', '.join(sorted(unknown))}"
                )
            profile = profiles.get(usage, {})
            for field, timeout in profile_patch.items():
                if timeout is None:
                    profile.pop(field, None)
                elif (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, int | float)
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    self._invalid(f"timeout_profiles.{usage}.{field} must be a positive number")
                else:
                    profile[field] = float(timeout)
            if profile:
                profiles[usage] = profile
            else:
                profiles.pop(usage, None)

        if profiles:
            llm["timeout_profiles"] = profiles
        else:
            llm.pop("timeout_profiles", None)

    def _align_interactive_model(self, llm: dict[str, Any], updates: Mapping[str, Any]) -> None:
        """Drop a ``routes.interactive.model`` left shadowing a base model patch.

        ``get_llm_config`` reports the interactive route's effective model, and the
        setup UI has no separate field for it -- so a top-level ``model`` patch is
        the user editing the model they were shown. A stale route override would
        silently win over it. A patch that names the route model itself is explicit
        intent and is left alone.
        """
        routes_patch = updates.get("routes")
        if isinstance(routes_patch, Mapping):
            interactive_patch = routes_patch.get("interactive")
            if isinstance(interactive_patch, Mapping) and "model" in interactive_patch:
                return
        routes = llm.get("routes")
        if not isinstance(routes, dict):
            return
        interactive = routes.get("interactive")
        if not isinstance(interactive, dict) or "model" not in interactive:
            return
        interactive.pop("model")
        if not interactive:
            routes.pop("interactive")
        if not routes:
            llm.pop("routes")

    def set_llm_config(self, *, updates: Mapping[str, Any]) -> dict[str, Any]:
        cfg = self._load_config()
        llm = cfg.setdefault("llm", {})
        if not isinstance(llm, dict):
            self._invalid("llm configuration must be a mapping")

        for field in _BASE_STRING_FIELDS:
            if field in updates:
                self._apply_string_patch(llm, field, updates[field], "llm")
        if _VENDOR_FIELD in updates:
            self._apply_string_patch(llm, _VENDOR_FIELD, updates[_VENDOR_FIELD], "llm")
        if "stream" in updates:
            stream = updates["stream"]
            if stream is None:
                llm.pop("stream", None)
            elif isinstance(stream, bool):
                llm["stream"] = stream
            else:
                self._invalid("llm.stream must be a boolean")
        if "max_output_tokens" in updates:
            max_output_tokens = updates["max_output_tokens"]
            if max_output_tokens is None:
                llm.pop("max_output_tokens", None)
            else:
                self._validate_max_output_tokens(max_output_tokens, "llm.max_output_tokens")
                llm["max_output_tokens"] = max_output_tokens
        if "context_window" in updates:
            context_window = updates["context_window"]
            if context_window is None:
                llm.pop("context_window", None)
            else:
                self._validate_context_window(context_window, "llm.context_window")
                llm["context_window"] = context_window
        if "routes" in updates:
            self._apply_routes_patch(llm, updates["routes"])
        if "models" in updates:
            self._apply_models_patch(llm, updates["models"])
        if "timeout_profiles" in updates:
            self._apply_timeout_profiles_patch(llm, updates["timeout_profiles"])
        if "model" in updates and updates["model"] is not None:
            self._align_interactive_model(llm, updates)

        try:
            save_yaml(self._config_path, cfg)
        except YamlConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = self.get_llm_config()
        result["status"] = "ok"
        return result
