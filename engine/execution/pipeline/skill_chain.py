from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Callable

from .gate import Gate, LLMGate
from .pipeline_context import output_key

logger = logging.getLogger(__name__)

# Gate and condition implementations are content, not engine code.  The
# registries start empty and are populated by load_gate_content(), which
# scans <agents_dir>/gates/**.py (module-level GATES) and
# <agents_dir>/conditions/**.py (module-level CONDITIONS).  from_yaml stays
# a pure lookup that fails loudly on an unknown key instead of silently
# degrading.
GATE_REGISTRY: dict[str, Callable[[], Gate]] = {}
CONDITION_REGISTRY: dict[str, Callable[[dict], bool]] = {}


@dataclass(frozen=True)
class GateContent:
    """Gate and condition factories loaded from one agents directory."""

    gates: dict[str, Callable[[], Gate]]
    conditions: dict[str, Callable[[dict], bool]]


_CONTENT_CACHE: dict[Path, GateContent] = {}


class GateContentError(ValueError):
    """Raised when gate/condition content files are invalid."""


def load_gate_content(agents_dir: Path) -> GateContent:
    """Populate the gate/condition registries from content directories.

    Scans ``<agents_dir>/gates/**/*.py`` for module-level ``GATES`` mappings
    and ``<agents_dir>/conditions/**/*.py`` for ``CONDITIONS``.  Idempotent
    per directory.  Invalid content fails loudly: a broken gate file must
    surface at startup, not as a confusing "unknown gate" at pipeline parse.
    """
    root = agents_dir.resolve()
    content = _CONTENT_CACHE.get(root)
    if content is None:
        gates: dict[str, Callable[[], Gate]] = {}
        conditions: dict[str, Callable[[dict], bool]] = {}
        _scan_content_dir(root / "gates", "GATES", gates)
        _scan_content_dir(root / "conditions", "CONDITIONS", conditions)
        content = GateContent(gates=gates, conditions=conditions)
        _CONTENT_CACHE[root] = content

    # Keep the historical module-level lookup API working for callers that do
    # not pass an explicit content scope. Runtime pipeline loading passes the
    # returned registries explicitly, so another project cannot overwrite an
    # already selected project's factories. The legacy view is first-wins for
    # duplicate names, while duplicate names within one project still fail in
    # _scan_content_dir above.
    for key, factory in content.gates.items():
        GATE_REGISTRY.setdefault(key, factory)
    for key, condition in content.conditions.items():
        CONDITION_REGISTRY.setdefault(key, condition)
    return content


def _scan_content_dir(content_dir: Path, attr: str, registry: dict) -> None:
    if not content_dir.is_dir():
        return
    for py_file in sorted(content_dir.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"agent_smith_content_{sha1(str(py_file.resolve()).encode()).hexdigest()}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                raise GateContentError(f"cannot load module spec for {py_file}")
            mod = importlib.util.module_from_spec(spec)
            # Content files stay independent from engine imports.  Expose the
            # stable output-key helper as an injected capability instead.
            mod.output_key = output_key
            # Some declarative content uses standard decorators (for example
            # dataclasses). They resolve annotations through sys.modules,
            # so register the transient module before executing it.
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        except GateContentError:
            sys.modules.pop(module_name, None)
            raise
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise GateContentError(f"failed to load gate content {py_file}: {exc}") from exc

        mapping = getattr(mod, attr, None)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            raise GateContentError(f"{py_file}: {attr} must be a dict")
        for key, factory in mapping.items():
            if not isinstance(key, str) or not key:
                raise GateContentError(f"{py_file}: {attr} keys must be non-empty strings")
            if not callable(factory):
                raise GateContentError(f"{py_file}: {attr}[{key!r}] must be callable")
            if key in registry and registry[key] is not factory:
                raise GateContentError(f"{py_file}: duplicate {attr} key {key!r}")
            registry[key] = factory


def _resolve_gate(
    gate_key: str,
    path: Path,
    where: str,
    registry: dict[str, Callable[[], Gate]] | None = None,
) -> Gate:
    gates = GATE_REGISTRY if registry is None else registry
    if gate_key not in gates:
        raise ValueError(
            f"{path.name}: unknown gate {gate_key!r} in {where}; "
            f"valid gates: {', '.join(sorted(gates))}"
        )
    gate_factory = gates[gate_key]
    gate = gate_factory() if callable(gate_factory) else gate_factory
    llm_prompt = getattr(gate, "llm_prompt", None)
    return LLMGate(gate, llm_prompt) if isinstance(llm_prompt, str) and llm_prompt else gate


@dataclass
class SkillNode:
    skill_name: str
    gate: Gate
    condition: Callable[[dict], bool] | None = None  # skip if returns False


class SkillChain:
    def __init__(
        self,
        nodes: list[SkillNode],
        backtrack_map: dict[str, str] | None = None,
        base_gates: list[Gate] | None = None,
    ) -> None:
        self.nodes = nodes
        self.backtrack_map: dict[str, str] = backtrack_map or {}
        # 兜底层：每个节点产出先过 base_gates，再过节点自己的 gate。
        # 由 pipeline YAML 顶层 base_gate/base_gates 声明，引擎不预置。
        self.base_gates: list[Gate] = list(base_gates or [])

    # ------------------------------------------------------------------
    # YAML-based pipeline loading
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        gate_registry: dict[str, Callable[[], Gate]] | None = None,
        condition_registry: dict[str, Callable[[dict], bool]] | None = None,
    ) -> SkillChain | None:
        """Build a SkillChain from a pipeline YAML definition file."""
        if not path.is_file():
            return None
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to parse pipeline yaml: %s", path)
            return None
        if not isinstance(data, dict):
            return None

        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            return None

        nodes: list[SkillNode] = []
        conditions = CONDITION_REGISTRY if condition_registry is None else condition_registry
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"{path.name}: steps[{index}] must be a mapping")
            skill_name = step.get("skill")
            if not isinstance(skill_name, str) or not skill_name:
                raise ValueError(f"{path.name}: steps[{index}].skill must be a non-empty string")
            gate_key = step.get("gate", "rubric")
            if not isinstance(gate_key, str) or not gate_key:
                raise ValueError(f"{path.name}: steps[{index}].gate must be a non-empty string")
            gate = _resolve_gate(gate_key, path, f"step {skill_name!r}", gate_registry)

            condition = None
            cond_key = step.get("condition")
            if cond_key is not None:
                if not isinstance(cond_key, str) or not cond_key:
                    raise ValueError(
                        f"{path.name}: steps[{index}].condition must be a non-empty string"
                    )
                if cond_key not in conditions:
                    raise ValueError(
                        f"{path.name}: unknown condition {cond_key!r} in step {skill_name!r}; "
                        f"valid conditions: {', '.join(sorted(conditions))}"
                    )
                condition = conditions[cond_key]

            nodes.append(SkillNode(skill_name=skill_name, gate=gate, condition=condition))

        if not nodes:
            return None

        base_keys = data.get("base_gates", data.get("base_gate"))
        if isinstance(base_keys, str):
            base_keys = [base_keys]
        base_gates: list[Gate] = []
        if base_keys is not None:
            if not isinstance(base_keys, list):
                raise ValueError(
                    f"{path.name}: base_gate(s) must be a gate key or a list of gate keys"
                )
            for key in base_keys:
                if not isinstance(key, str):
                    raise ValueError(f"{path.name}: base_gate entries must be strings")
                base_gates.append(_resolve_gate(key, path, "base_gates", gate_registry))

        backtrack = data.get("backtrack")
        if backtrack is not None and not isinstance(backtrack, dict):
            raise ValueError(f"{path.name}: backtrack must be a mapping")
        backtrack_map = dict(backtrack or {})
        return cls(nodes=nodes, backtrack_map=backtrack_map, base_gates=base_gates)

    @classmethod
    def load_pipelines(
        cls,
        pipelines_dir: Path,
        *,
        gate_registry: dict[str, Callable[[], Gate]] | None = None,
        condition_registry: dict[str, Callable[[dict], bool]] | None = None,
    ) -> dict[str, "SkillChain"]:
        """Load all pipeline YAML files from a directory, keyed by route name."""
        result: dict[str, SkillChain] = {}
        if not pipelines_dir.is_dir():
            return result
        for yaml_file in sorted(pipelines_dir.glob("*.yaml")):
            chain = cls.from_yaml(
                yaml_file,
                gate_registry=gate_registry,
                condition_registry=condition_registry,
            )
            if chain is None:
                continue
            try:
                import yaml
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                route = data.get("route", yaml_file.stem)
            except Exception:
                logger.warning(
                    "failed to read route key from %s; falling back to file stem",
                    yaml_file, exc_info=True,
                )
                route = yaml_file.stem
            if not isinstance(route, str) or not route:
                # 非字符串 route（如 `route: 123`）按原样入库后，字符串
                # pipeline_id 永远查不到它，只会报一句误导性的 missing pipeline。
                logger.warning(
                    "pipeline %s declares non-string route %r; using file stem",
                    yaml_file.name, route,
                )
                route = yaml_file.stem
            if route in result:
                logger.warning(
                    "duplicate pipeline route %r: %s overrides an earlier definition",
                    route, yaml_file.name,
                )
            result[route] = chain
        return result
