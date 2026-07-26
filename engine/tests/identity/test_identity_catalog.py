from __future__ import annotations

from pathlib import Path

import pytest

from engine.identity import IdentityCatalog, IdentityCatalogError


def _write_identity(directory: Path, name: str, *lines: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("\n".join(lines), encoding="utf-8")


def test_catalog_routes_across_multiple_domain_identities(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "smith.yaml",
        "schema: agentsmith.identity/v1",
        "id: smith",
        "name: Smith",
        "default: true",
        "routes:",
        "  - id: feature",
        "    keywords: [实现]",
        "    pipeline: feature",
    )
    _write_identity(
        tmp_path,
        "legal.yaml",
        "schema: agentsmith.identity/v1",
        "id: legal",
        "name: 法务助手",
        "prompt:",
        "  role: 审慎处理合同与合规问题。",
        "tools:",
        "  enabled: [read_file, web_search]",
        "skills:",
        "  enabled: [contract-review]",
        "routes:",
        "  - id: contract_review",
        "    examples: [审查这份合同]",
        "    keywords: [合同, 违约]",
        "    pipeline: legal-contract-review",
        "    priority: 5",
    )

    catalog = IdentityCatalog.load(tmp_path)
    decision = catalog.resolve("请审查这份合同，并找出违约风险")

    assert decision.identity_id == "legal"
    assert decision.route_id == "contract_review"
    assert decision.pipeline_id == "legal-contract-review"
    assert decision.identity.enabled_tools == ("read_file", "web_search")


def test_catalog_uses_default_identity_direct_fallback(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "smith.yaml",
        "schema: agentsmith.identity/v1",
        "id: smith",
        "name: Smith",
        "default: true",
        "routes: []",
    )

    decision = IdentityCatalog.load(tmp_path).resolve("今天天气怎么样")

    assert decision.identity_id == "smith"
    assert decision.route_id == "direct"
    assert decision.pipeline_id is None


def test_catalog_rejects_unresolvable_pipeline_and_explicit_skill_references(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "smith.yaml",
        "schema: agentsmith.identity/v1",
        "id: smith",
        "name: Smith",
        "default: true",
        "skills:",
        "  enabled: [contract-review]",
        "routes:",
        "  - id: contract_review",
        "    keywords: [合同]",
        "    pipeline: legal-contract-review",
    )

    catalog = IdentityCatalog.load(tmp_path)

    with pytest.raises(IdentityCatalogError, match="unknown pipeline"):
        catalog.validate_assets(set(), {"contract-review"})
    with pytest.raises(IdentityCatalogError, match="enables unknown skills"):
        catalog.validate_assets(
            {"legal-contract-review"},
            set(),
        )
