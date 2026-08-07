"""Canonical description of the user-editable LLM configuration fields.

The same field list used to exist in four places that were not quite the same
list: the engine's route projection, the server's accepted-field set, the
server's publicly readable set (which must exclude write-only secrets), and the
HTTP patch schema.  A field missing from any one of them failed *silently* —
a route-scoped value would simply never reach the adapter.

Declare a field once here.  The engine and the server derive their own
projections from the flags below, and a conformance test keeps the HTTP schema
honest, since Pydantic models are worth writing out explicitly for their
validators and generated docs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["string", "bool", "positive_int", "usage_name"]


@dataclass(frozen=True, slots=True)
class LLMConfigField:
    """One user-editable configuration field and where it is allowed to appear."""

    name: str
    kind: FieldKind
    #: Value used when neither the route nor the base configuration supplies one.
    default: Any = ""
    #: Write-only: accepted on patches, never returned by the read API.
    secret: bool = False
    #: May be overridden per entry under ``llm.routes`` / ``llm.models``.
    route_scoped: bool = True
    #: Survives ``resolve_llm_config``'s projection into an adapter config.
    engine_projected: bool = True


LLM_CONFIG_FIELDS: tuple[LLMConfigField, ...] = (
    LLMConfigField("provider", "string"),
    LLMConfigField("api_key", "string", secret=True),
    LLMConfigField("base_url", "string"),
    LLMConfigField("model", "string"),
    LLMConfigField("stream", "bool", default=True),
    LLMConfigField("thinking", "bool", default=False),
    LLMConfigField("max_output_tokens", "positive_int", default=None),
    LLMConfigField("context_window", "positive_int", default=None),
    # Selects a shared timeout profile; resolved into `timeout` before the
    # adapter sees it, so it is deliberately not projected.
    LLMConfigField("timeout_profile", "usage_name", default=None, engine_projected=False),
    # Supplier display name. Top-level only: it is identity metadata, never a
    # per-route override and never part of an adapter request.
    LLMConfigField("vendor", "string", route_scoped=False, engine_projected=False),
)


def _names(**flags: Any) -> tuple[str, ...]:
    return tuple(
        field.name
        for field in LLM_CONFIG_FIELDS
        if all(getattr(field, flag) == value for flag, value in flags.items())
    )


#: Fields an ``llm.routes.<usage>`` entry may carry.
ROUTE_FIELDS: tuple[str, ...] = _names(route_scoped=True)
#: Route fields the read API may return — secrets are write-only.
PUBLIC_ROUTE_FIELDS: tuple[str, ...] = _names(route_scoped=True, secret=False)
#: Route fields carried through into the resolved adapter configuration.
ENGINE_ROUTE_FIELDS: tuple[str, ...] = _names(engine_projected=True)
#: Route fields patched as plain strings.
ROUTE_STRING_FIELDS: tuple[str, ...] = _names(route_scoped=True, kind="string")
#: Route fields patched as booleans.
ROUTE_BOOL_FIELDS: tuple[str, ...] = _names(route_scoped=True, kind="bool")
#: Top-level-only fields, handled outside the route projection.
BASE_ONLY_FIELDS: tuple[str, ...] = _names(route_scoped=False)

#: Fallbacks applied by the engine's route projection.
ROUTE_DEFAULTS: dict[str, Any] = {
    field.name: field.default
    for field in LLM_CONFIG_FIELDS
    if field.engine_projected
}
