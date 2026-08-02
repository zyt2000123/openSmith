"""Local bearer-token authentication for the Agent-Smith API.

The server generates a random token on first startup and persists it to
~/.agent-smith/auth_token (mode 0600).  Every /api/* request must carry
``Authorization: Bearer <token>``.  The health endpoint is exempt.

The shell (or any local client) reads that file to authenticate.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common import config as common_config
from common.paths import PRIVATE_FILE_MODE

_TOKEN_PATH: Path | None = None
_bearer_scheme = HTTPBearer(auto_error=False)

_cached_token: str | None = None
_cached_token_path: Path | None = None


def _token_path() -> Path:
    return _TOKEN_PATH if _TOKEN_PATH is not None else common_config.PATHS.data_dir / "auth_token"


def _read_or_create_token() -> str:
    global _cached_token, _cached_token_path
    token_path = _token_path()
    if _cached_token is not None and _cached_token_path == token_path:
        return _cached_token

    if token_path.is_file():
        token = token_path.read_text().strip()
        if token:
            _cached_token = token
            _cached_token_path = token_path
            return token

    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(token_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    _cached_token = token
    _cached_token_path = token_path
    return token


def get_local_token() -> str:
    return _read_or_create_token()


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    if credentials is None or not hmac.compare_digest(
        credentials.credentials, get_local_token()
    ):
        raise HTTPException(401, "Invalid or missing auth token")
