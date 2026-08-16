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


# Legacy module-level constants for backward compatibility.
# These resolve lazily through module attribute lookup; only names with live
# consumers are kept — everything else reads a property off ``config.PATHS``.
def __getattr__(name: str):
    """Resolve legacy path exports lazily from the current ``AppPaths`` instance."""
    paths = _get_paths()
    mapping = {
        "DATA_DIR": paths.data_dir,
        "AGENT_DIR": paths.agent_dir,
        "SMITH_PROFILE_DIR": paths.smith_profile_dir,
        "BUILTIN_SKILLS_DIR": paths.builtin_skills_dir,
        "PATHS": paths,
    }
    if name in mapping:
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
