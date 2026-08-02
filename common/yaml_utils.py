from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .paths import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE, _ensure_real_path


class YamlConfigError(ValueError):
    """Raised when a configuration YAML document is invalid or unsafe to persist."""


def _ensure_private_parent(path: Path) -> None:
    _ensure_real_path(path, label="YAML path")
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(f"YAML parent is not a directory: {current}")

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            if directory.is_symlink():
                raise RuntimeError(f"Refusing to use symlinked YAML path: {directory}")
            if not directory.is_dir():
                raise NotADirectoryError(f"YAML parent is not a directory: {directory}")
        else:
            directory.chmod(PRIVATE_DIR_MODE)


def load_yaml(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise YamlConfigError(f"Invalid YAML in {p}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise YamlConfigError(f"YAML root in {p} must be a mapping")
    return data


def save_yaml(path: Path | str, data: Any) -> None:
    p = Path(path)
    _ensure_real_path(p, label="YAML path")
    if not isinstance(data, dict):
        raise YamlConfigError(f"YAML root for {p} must be a mapping")
    try:
        content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError as exc:
        raise YamlConfigError(f"Unable to serialize YAML for {p}") from exc

    _ensure_private_parent(p.parent)
    fd, temp_name = tempfile.mkstemp(
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, PRIVATE_FILE_MODE)
        os.replace(temp_path, p)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def merge_configs(*configs: dict[str, Any]) -> dict[str, Any]:
    """Deep merge dicts. Later overrides earlier."""
    result: dict[str, Any] = {}
    for cfg in configs:
        for key, value in cfg.items():
            if value is None:
                continue
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = merge_configs(result[key], value)
            else:
                result[key] = value
    return result
