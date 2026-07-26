"""Declarative identity selection for the resident Smith agent.

Callers import the identity contracts from this package. YAML parsing,
validation, and route scoring remain implementation details of the catalog
module.
"""

from .catalog import (
    IdentityCatalog,
    IdentityCatalogError,
    IdentitySpec,
    RouteDecision,
    RouteSpec,
    load_identity_catalog,
)

__all__ = (
    "IdentityCatalog",
    "IdentityCatalogError",
    "IdentitySpec",
    "RouteDecision",
    "RouteSpec",
    "load_identity_catalog",
)
