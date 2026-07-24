"""Session state checkpoint for crash recovery.

Saves execution state to a JSON file after each significant step.
``restore()`` returns the last checkpoint; agent_loop consumes it on the
next identical request to resume a crash-interrupted chain (stale
checkpoints from a different request are cleared instead).
"""

import json
import os
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass, asdict

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


@dataclass
class SessionCheckpoint:
    agent_id: str
    session_id: str
    identity_id: str
    route_id: str
    skill_chain_index: int  # -1 for DIRECT
    context: dict  # accumulated skill outputs
    timestamp: str
    # Checkpoints are project-scoped.  An empty value represents a legacy
    # checkpoint and is intentionally not eligible for resume.
    working_dir: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionCheckpoint":
        return cls(**d)


class SessionStateManager:
    """Persist and restore session execution state."""

    def __init__(self, agent_dir: Path):
        agent_root = agent_dir.resolve()
        sessions_dir = agent_root / "sessions"
        if sessions_dir.is_symlink():
            raise ValueError("session directory must not be a symlink")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir = sessions_dir / ".state"
        if self._state_dir.is_symlink():
            raise ValueError("session state directory must not be a symlink")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir = self._state_dir.resolve()
        if not self._state_dir.is_relative_to(agent_root):
            raise ValueError("session state directory escapes agent directory")
        self._state_dir.chmod(_PRIVATE_DIR_MODE)

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")
        p = (self._state_dir / f"{session_id}.json").resolve()
        if not p.is_relative_to(self._state_dir.resolve()):
            raise ValueError("session_id escapes state dir")
        return p

    def save(self, checkpoint: SessionCheckpoint) -> None:
        path = self._path(checkpoint.session_id)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            path.chmod(_PRIVATE_FILE_MODE)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def restore(self, session_id: str) -> SessionCheckpoint | None:
        try:
            path = self._path(session_id)
        except ValueError:
            # 非法 id（如 list_active 撞到带点号的杂散文件名）读不出
            # checkpoint 是正常结果，不应让整个恢复流程崩溃。
            return None
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SessionCheckpoint.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def clear(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.is_file():
            path.unlink()

    def list_active(self) -> list[SessionCheckpoint]:
        """List all sessions with saved state (crashed/interrupted)."""
        results = []
        for f in self._state_dir.glob("*.json"):
            cp = self.restore(f.stem)
            if cp:
                results.append(cp)
        return results
