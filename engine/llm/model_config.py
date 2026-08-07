from __future__ import annotations

import ipaddress
import math
import os
import socket
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common import config as common_config
from common.yaml_utils import YamlConfigError, load_yaml, merge_configs

from .contracts import (
    LLMProviderConfig,
    LLMTimeouts,
    UnsupportedProviderError,
)
from .factory import create_llm_client, normalize_provider_name
from .port import LLMPort

SMITH_TEMPLATE_ID = "personal-assistant"

# Optional test overrides.  Production resolution deliberately goes through
# ``common.config.PATHS`` at call time so ``reset_paths()`` takes effect even
# after this module has already been imported.
DATA_DIR: Path | None = None
SMITH_PROFILE_DIR: Path | None = None
AGENT_DIR: Path | None = None


def _config_paths() -> tuple[Path, Path, Path]:
    paths = common_config.PATHS
    return (
        DATA_DIR if DATA_DIR is not None else paths.data_dir,
        SMITH_PROFILE_DIR if SMITH_PROFILE_DIR is not None else paths.smith_profile_dir,
        AGENT_DIR if AGENT_DIR is not None else paths.agent_dir,
    )

# Deep-merge of the three static config levels, cached per file fingerprint
# (path + mtime + size).  Config resolution runs on every model route lookup,
# so re-reading unchanged YAML files on each call is wasted disk I/O; edits to
# any level are still picked up because the fingerprint changes.  Environment
# overrides (AGENTSMITH_LLM_*) are the lowest-precedence layer and are part of
# the fingerprint so a change invalidates the cache.
_BASE_MERGE_CACHE: tuple[tuple[object, ...], dict[str, Any]] | None = None

_ENV_LLM_KEYS = (
    ("AGENTSMITH_LLM_API_KEY", "api_key"),
    ("AGENTSMITH_LLM_BASE_URL", "base_url"),
    ("AGENTSMITH_LLM_MODEL", "model"),
    ("AGENTSMITH_LLM_PROVIDER", "provider"),
)


def _env_defaults() -> dict[str, Any]:
    """LLM settings sourced from the environment, as the lowest-precedence layer."""
    env_llm: dict[str, str] = {}
    for env_key, cfg_key in _ENV_LLM_KEYS:
        val = os.environ.get(env_key)
        if val:
            env_llm[cfg_key] = val
    if not env_llm:
        return {}
    return {"llm": env_llm}


def _file_fingerprint(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), 0, 0)


def _merged_base_config() -> dict[str, Any]:
    """Merge env/platform/seed/runtime config levels, cached until something changes."""
    global _BASE_MERGE_CACHE
    data_dir, smith_profile_dir, agent_dir = _config_paths()
    env = _env_defaults()
    fingerprint: tuple[object, ...] = (
        # The loader identity keeps the cache honest when tests replace
        # load_yaml with a stub; the file fingerprints catch real edits.
        id(load_yaml),
        tuple(sorted(env.get("llm", {}).items())),
        *(
            _file_fingerprint(path)
            for path in (
                data_dir / "config.yaml",
                smith_profile_dir / "config.yaml",
                agent_dir / "config.yaml",
            )
        ),
    )
    if _BASE_MERGE_CACHE is not None and _BASE_MERGE_CACHE[0] == fingerprint:
        return _BASE_MERGE_CACHE[1]
    merged = merge_configs(
        env,
        load_yaml(data_dir / "config.yaml"),
        load_yaml(smith_profile_dir / "config.yaml"),
        load_yaml(agent_dir / "config.yaml"),
    )
    _BASE_MERGE_CACHE = (fingerprint, merged)
    return merged


class LLMUsage(str, Enum):
    """Caller intent used to select an LLM route and timeout profile."""

    INTERACTIVE = "interactive"
    GATE = "gate"
    BACKGROUND = "background"


_TIMEOUT_FIELDS = frozenset({"connect", "read", "stream_read", "write", "pool"})
_TIMEOUT_DEFAULTS: dict[LLMUsage, dict[str, float]] = {
    LLMUsage.INTERACTIVE: {
        "connect": 10.0,
        "read": 90.0,
        "stream_read": 120.0,
        "write": 30.0,
        "pool": 10.0,
    },
    LLMUsage.GATE: {
        "connect": 10.0,
        "read": 90.0,
        "stream_read": 90.0,
        "write": 30.0,
        "pool": 10.0,
    },
    LLMUsage.BACKGROUND: {
        "connect": 10.0,
        "read": 240.0,
        "stream_read": 300.0,
        "write": 30.0,
        "pool": 10.0,
    },
}
_ROUTE_FIELDS = (
    "api_key",
    "base_url",
    "model",
    "provider",
    "stream",
    "max_output_tokens",
    "context_window",
)
_REMOVED_LEGACY_USAGE = "vision"


def normalize_legacy_llm_config(llm: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with settings for the removed visual route discarded.

    Configuration is resolved directly by both the server and the engine, so
    this compatibility step belongs at their shared boundary rather than only
    in the settings API.  It intentionally preserves malformed values for the
    normal validation path to report, while allowing an existing config to
    shed obsolete route data on its next ordinary save.
    """
    normalized = dict(llm)

    for section in ("routes", "timeout_profiles"):
        values = normalized.get(section)
        if not isinstance(values, dict) or _REMOVED_LEGACY_USAGE not in values:
            continue
        cleaned = {
            name: value
            for name, value in values.items()
            if name != _REMOVED_LEGACY_USAGE
        }
        if cleaned:
            normalized[section] = cleaned
        else:
            normalized.pop(section, None)

    for section in ("routes", "models"):
        values = normalized.get(section)
        if not isinstance(values, dict):
            continue
        cleaned: dict[str, Any] = {}
        changed = False
        for name, route in values.items():
            if isinstance(route, dict) and route.get("timeout_profile") == _REMOVED_LEGACY_USAGE:
                route = dict(route)
                route.pop("timeout_profile")
                changed = True
            cleaned[name] = route
        if changed:
            normalized[section] = cleaned

    return normalized


def validate_llm_base_url(value: object) -> str:
    """Validate the credential-bearing endpoint used by every LLM entry path."""
    if not isinstance(value, str) or not value.strip():
        raise YamlConfigError("LLM base_url must be a non-empty string")
    base_url = value.strip()
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise YamlConfigError("LLM base_url must be an HTTPS URL with a hostname")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise YamlConfigError("LLM base_url must not contain credentials, a query, or a fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise YamlConfigError("LLM base_url must not target a private or local IP address")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        # Leave unresolved public hostnames to the request path, which can
        # report a useful transport error. Any address that does resolve must
        # be public before credentials can be sent to it.
        return base_url

    for candidate in addresses:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not address.is_global:
            raise YamlConfigError("LLM base_url must not target a private or local IP address")
    return base_url


def build_llm_client(config: dict) -> LLMPort:
    """Build the normalized LLM Interface from a merged configuration dict.

    Resolution deliberately tolerates an empty configuration so optional
    background jobs can elect not to run.  Construction is the boundary where
    a caller has committed to making a provider request, so reject incomplete
    credentials here with a configuration error rather than failing later in
    httpx with an opaque URL or authentication error.
    """
    if not isinstance(config, dict):
        raise YamlConfigError("LLM configuration must be a mapping")

    provider_value = config.get("provider", "")
    try:
        provider = normalize_provider_name(provider_value)
    except UnsupportedProviderError as exc:
        raise YamlConfigError(str(exc)) from exc

    base_url = validate_llm_base_url(config.get("base_url"))

    required_values = {
        "api_key": config.get("api_key"),
        "base_url": base_url,
        "model": config.get("model"),
    }
    missing = [
        field
        for field, value in required_values.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        fields = ", ".join(missing)
        raise YamlConfigError(f"LLM configuration is missing required fields: {fields}")

    stream = config.get("stream", True)
    if not isinstance(stream, bool):
        raise YamlConfigError("LLM stream configuration must be a boolean")

    max_output_tokens = config.get("max_output_tokens")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise YamlConfigError("LLM max_output_tokens must be a positive integer")

    context_window = config.get("context_window")
    if context_window is not None and (
        isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or context_window <= 0
    ):
        raise YamlConfigError("LLM context_window must be a positive integer")

    timeout = config.get("timeout")
    if timeout is None:
        timeouts = None
    elif isinstance(timeout, LLMTimeouts):
        timeouts = timeout
    elif isinstance(timeout, dict):
        unknown_timeout_fields = set(timeout) - _TIMEOUT_FIELDS
        if unknown_timeout_fields:
            names = ", ".join(sorted(unknown_timeout_fields))
            raise YamlConfigError(f"Unknown LLM timeout fields: {names}")
        try:
            timeouts = LLMTimeouts(**timeout)
        except TypeError as exc:
            raise YamlConfigError("Invalid LLM timeout configuration") from exc
    else:
        raise YamlConfigError("LLM timeout configuration must be a mapping")

    resolved_timeouts = timeouts or LLMTimeouts()
    for field in _TIMEOUT_FIELDS:
        value = getattr(resolved_timeouts, field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise YamlConfigError(f"LLM timeout {field} must be a positive number")

    return create_llm_client(LLMProviderConfig(
        provider=provider,
        api_key=config["api_key"].strip(),
        base_url=base_url,
        model=config["model"].strip(),
        stream=stream,
        timeouts=resolved_timeouts,
        max_output_tokens=max_output_tokens,
        context_window=context_window,
    ))


def _as_usage(value: LLMUsage | str) -> LLMUsage:
    if isinstance(value, LLMUsage):
        return value
    try:
        return LLMUsage(value)
    except ValueError as exc:
        allowed = ", ".join(usage.value for usage in LLMUsage)
        raise YamlConfigError(f"Unknown LLM usage {value!r}; expected one of: {allowed}") from exc


def _mapping(value: object, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise YamlConfigError(f"{label} must be a mapping")
    return value


def _resolve_timeout(
    llm: dict[str, Any],
    usage: LLMUsage,
    route: dict[str, Any],
) -> dict[str, float]:
    profile_name = route.get("timeout_profile", usage.value)
    if not isinstance(profile_name, str):
        raise YamlConfigError("LLM timeout_profile must be a string")
    try:
        profile = LLMUsage(profile_name)
    except ValueError as exc:
        allowed = ", ".join(usage.value for usage in LLMUsage)
        raise YamlConfigError(
            f"Unknown LLM timeout profile {profile_name!r}; expected one of: {allowed}"
        ) from exc

    timeout_profiles = _mapping(llm.get("timeout_profiles"), "LLM timeout_profiles")
    override = _mapping(
        timeout_profiles.get(profile.value),
        f"LLM timeout profile {profile.value!r}",
    )
    unknown = set(override) - _TIMEOUT_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise YamlConfigError(f"Unknown LLM timeout fields: {names}")

    resolved = dict(_TIMEOUT_DEFAULTS[profile])
    resolved.update(override)
    for name, value in resolved.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise YamlConfigError(f"LLM timeout {profile.value}.{name} must be a positive number")
        resolved[name] = float(value)
    return resolved


def resolve_price_table() -> dict[str, dict[str, float]]:
    """Return the optional ``llm.pricing`` table (USD per million tokens).

    Keys are model names; each value may price ``input``, ``output``,
    ``cache_read`` and ``cache_write`` tokens.  Prices always come from local
    configuration — never from a gateway or provider API.
    """
    merged = _merged_base_config()
    llm = merged.get("llm")
    pricing = llm.get("pricing") if isinstance(llm, dict) else None
    if not isinstance(pricing, dict):
        return {}
    table: dict[str, dict[str, float]] = {}
    for model, prices in pricing.items():
        if not isinstance(prices, dict):
            continue
        entry: dict[str, float] = {}
        for key in ("input", "output", "cache_read", "cache_write"):
            value = prices.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                entry[key] = float(value)
        if entry:
            table[str(model)] = entry
    return table


def resolve_llm_config(
    session_override: dict[str, Any] | None = None,
    usage: LLMUsage | str = LLMUsage.INTERACTIVE,
    model_profile: str | None = None,
) -> dict[str, Any]:
    """Return the selected LLM route after merging config levels.

    Configuration files are the only source of route settings.  Levels (lower
    overrides upper):
      1. Platform:  ~/.agent-smith/config.yaml
      2. Smith seed: agents/smith/config.yaml
      3. Smith runtime: ~/.agent-smith/agent/config.yaml
      4. Session:   dict passed at runtime

    ``llm.routes`` may override the base config for ``interactive``, ``gate``,
    ``background``.  Omitted routes inherit the base model unchanged.
    ``model_profile`` selects a named entry from ``llm.models`` for the
    interactive route; the named profile has precedence over the route/base
    model fields while timeout profiles remain shared.
    """
    selected_usage = _as_usage(usage)
    merged = merge_configs(_merged_base_config(), session_override or {})

    llm = merged.get("llm", merged)
    if not isinstance(llm, dict):
        raise YamlConfigError("LLM configuration must be a mapping")
    llm = normalize_legacy_llm_config(llm)

    selected_profile: dict[str, Any] = {}
    if model_profile is not None:
        if not isinstance(model_profile, str) or not model_profile.strip():
            raise YamlConfigError("LLM model profile must be a non-empty string")
        models = _mapping(llm.get("models"), "LLM models")
        profile = models.get(model_profile)
        if not isinstance(profile, dict):
            raise YamlConfigError(f"Unknown LLM model profile: {model_profile}")
        selected_profile = profile

    routes = _mapping(llm.get("routes"), "LLM routes")
    route = _mapping(routes.get(selected_usage.value), f"LLM route {selected_usage.value!r}")
    defaults: dict[str, Any] = {
        "provider": "",
        "stream": True,
        "max_output_tokens": None,
        "context_window": None,
    }
    selected = {
        field: route[field] if field in route else llm.get(field, defaults.get(field, ""))
        for field in _ROUTE_FIELDS
    }
    if selected_profile:
        selected.update({field: selected_profile[field] for field in _ROUTE_FIELDS if field in selected_profile})
    # Supplier identity is display metadata only. It deliberately stays out of
    # route overrides and adapter construction, but must reach runtime prompt
    # metadata for truthful model identity responses.
    if "vendor" in llm:
        selected["vendor"] = llm["vendor"]
    selected["timeout"] = _resolve_timeout(llm, selected_usage, route)
    return selected
