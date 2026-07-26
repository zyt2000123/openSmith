"""Guard automatic package discovery against silent wheel drift."""

from __future__ import annotations

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2]


def _discovered_packages() -> set[str]:
    config = tomllib.loads((ENGINE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    find = config["tool"]["setuptools"]["packages"]["find"]
    search_root = (ENGINE_ROOT / find["where"][0]).resolve()
    packages: set[str] = set()
    for init in search_root.rglob("__init__.py"):
        parts = init.parent.relative_to(search_root).parts
        if any(
            not (search_root.joinpath(*parts[:index]) / "__init__.py").is_file()
            for index in range(1, len(parts) + 1)
        ):
            continue
        package = ".".join(parts)
        if not any(fnmatchcase(package, pattern) for pattern in find["include"]):
            continue
        if any(fnmatchcase(package, pattern) for pattern in find["exclude"]):
            continue
        packages.add(package)
    return packages


def _actual_packages() -> set[str]:
    """Every importable package under engine/, excluding tests and virtualenvs."""
    found: set[str] = set()
    for init in ENGINE_ROOT.rglob("__init__.py"):
        parts = init.parent.relative_to(ENGINE_ROOT).parts
        if any(part.startswith(".") or part == "tests" for part in parts):
            continue
        found.add(".".join(("engine",) + parts))
    return found


def test_declared_packages_match_the_filesystem() -> None:
    declared = _discovered_packages()
    actual = _actual_packages()

    assert declared == actual, (
        f"missing from pyproject: {sorted(actual - declared)}; "
        f"declared but absent on disk: {sorted(declared - actual)}"
    )
