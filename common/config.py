from .paths import AppPaths

_paths_instance: AppPaths | None = None


def _get_paths() -> AppPaths:
    """Lazy initialization: allows runtime environment changes before first access."""
    global _paths_instance
    if _paths_instance is None:
        _paths_instance = AppPaths.defaults()
    return _paths_instance


def reset_paths(paths: AppPaths | None = None) -> None:
    """Replace the lazy path instance for tests or runtime reconfiguration.

    Later ``config.PATHS`` lookups observe the replacement.  A value imported
    with ``from common.config import PATHS`` is an ordinary Python snapshot and
    intentionally keeps the instance that was bound at import time.
    """
    global _paths_instance
    _paths_instance = paths


# Legacy module-level constants for backward compatibility
# These resolve lazily through module attribute lookup.
def __getattr__(name: str):
    """Resolve legacy path exports lazily from the current ``AppPaths`` instance."""
    paths = _get_paths()
    mapping = {
        "DATA_DIR": paths.data_dir,
        "AGENT_DIR": paths.agent_dir,
        "SQLITE_PATH": paths.sqlite_path,
        "SMITH_PROFILE_DIR": paths.smith_profile_dir,
        "BUILTIN_SKILLS_DIR": paths.builtin_skills_dir,
        "BUILTIN_TOOLS_DIR": paths.builtin_tools_dir,
        "BUILTIN_IDENTITIES_DIR": paths.builtin_identities_dir,
        "SAFETY_RULES_PATH": paths.safety_rules_path,
        "PATHS": paths,
    }
    if name in mapping:
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ensure_dirs() -> None:
    _get_paths().ensure_base_dirs()
