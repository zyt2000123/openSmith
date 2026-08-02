"""Persisted enablement state for the resident agent's skill catalog."""

from __future__ import annotations

from pathlib import Path

from common.yaml_utils import YamlConfigError, load_yaml, save_yaml

_SETTINGS_FILENAME = "skills.yaml"


class SkillSettingsError(ValueError):
    """Raised when the skill enablement settings are malformed."""


def _settings_path(agent_dir: Path) -> Path:
    return agent_dir / _SETTINGS_FILENAME


def disabled_skill_names(agent_dir: Path) -> set[str]:
    try:
        settings = load_yaml(_settings_path(agent_dir))
    except YamlConfigError as exc:
        raise SkillSettingsError(str(exc)) from exc

    unknown = set(settings) - {"disabled"}
    if unknown:
        raise SkillSettingsError(f"unknown settings: {', '.join(sorted(unknown))}")

    disabled = settings.get("disabled", [])
    if not isinstance(disabled, list) or any(not isinstance(name, str) or not name.strip() for name in disabled):
        raise SkillSettingsError("disabled must be a list of non-empty skill names")
    return {name.strip() for name in disabled}


def set_skill_enabled(agent_dir: Path, skill_name: str, *, enabled: bool) -> set[str]:
    stripped = skill_name.strip()
    if not stripped:
        raise SkillSettingsError("skill name must not be empty")

    disabled = disabled_skill_names(agent_dir)
    if enabled:
        disabled.discard(stripped)
    else:
        disabled.add(stripped)
    save_yaml(_settings_path(agent_dir), {"disabled": sorted(disabled)})
    return disabled
