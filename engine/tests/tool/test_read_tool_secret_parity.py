"""Never-approval read tools must not leak what ToolGuard gates on a direct read.

``grep``/``list_dir``/``glob_files`` declare ``approval_policy:"never"`` and walk
a whole subtree.  ToolGuard only reasons about the path the model *names*, so a
sensitive file the guard would gate on a direct ``read_file`` is reachable
through any of these when a parent directory is named instead.  Each tool
therefore carries its own credential-name exclusion.  ``agents/`` cannot import
the engine, so those exclusions are *mirrored* from
``engine.safety.tool_guard._is_sensitive_read_name`` rather than shared — and
this test is what keeps the mirror from drifting.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from engine.safety.tool_guard import _is_sensitive_read_name

ROOT = Path(__file__).resolve().parents[3]


def _load_tool(name: str):
    path = ROOT / "agents" / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Names the guard treats as sensitive on a direct read; each read-walking tool
# must exclude every one of them.  Includes the affixed SSH-key copies that were
# the confirmed bypass (a bare-basename list missed them).
_GUARD_SENSITIVE = [
    "id_rsa", "id_rsa_old", "id_rsa.bak", "backup-id_ed25519", "id_ed25519_work",
    "id_ecdsa_prod", "id_dsa",
    ".env", ".env.local", ".env.production",
    "server.key", "prod.pem", "cert.p12", "bundle.pfx",
]
_GUARD_ORDINARY = [
    "app.py", "README.md", "id_rsa.pub", ".env.example", "grid.py", "keyboard.ts",
]


def test_guard_fixture_matches_the_guard_predicate() -> None:
    """Guardrail on the fixtures themselves, so a guard change is caught here."""
    for name in _GUARD_SENSITIVE:
        assert _is_sensitive_read_name(name), name
    for name in _GUARD_ORDINARY:
        assert not _is_sensitive_read_name(name), name


def test_list_dir_and_glob_predicate_covers_everything_the_guard_gates() -> None:
    list_dir = _load_tool("list_dir")
    glob_files = _load_tool("glob_files")
    for name in _GUARD_SENSITIVE:
        assert list_dir._is_secret_name(name), f"list_dir must exclude {name}"
        assert glob_files._is_secret_name(name), f"glob_files must exclude {name}"
    # Public keys and env templates the guard treats as ordinary must remain
    # listable, or the tools would over-hide benign files.
    for name in ("id_rsa.pub", ".env.example", "app.py"):
        assert not list_dir._is_secret_name(name), name
        assert not glob_files._is_secret_name(name), name


# A token planted in both a safe file and the secret files.  A directory walk
# that respects the exclusion matches it only in the safe file; the search
# pattern deliberately avoids these strings so no assertion trips on the
# pattern-echo in a "No matches" message.
_SHARED_TOKEN = "needle_zzz"


def _make_secret_tree(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text(f"# {_SHARED_TOKEN} in ordinary source\n")
    (workspace / "id_rsa_old").write_text(f"PRIVATE-KEY {_SHARED_TOKEN}\n")
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "prod.key").write_text(f"PRIVATE-KEY {_SHARED_TOKEN}\n")
    ssh = workspace / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text(f"OPENSSH-PRIVATE-KEY {_SHARED_TOKEN}\n")
    return workspace


def test_grep_directory_walk_does_not_dump_affixed_private_keys(tmp_path) -> None:
    grep = _load_tool("grep")
    workspace = _make_secret_tree(tmp_path)

    result = asyncio.run(grep.execute(pattern=_SHARED_TOKEN, path=str(workspace)))

    # The safe file matches; the secret files are excluded from the walk.
    assert "app.py" in result, result
    assert "id_rsa_old" not in result
    assert "prod.key" not in result
    assert "PRIVATE-KEY" not in result
    assert "OPENSSH-PRIVATE-KEY" not in result


def test_grep_can_still_search_an_affixed_key_named_directly(tmp_path) -> None:
    """Over-exclusion only applies to directory walks; a named file is governed
    by ToolGuard, not by this tool."""
    grep = _load_tool("grep")
    workspace = _make_secret_tree(tmp_path)

    result = asyncio.run(grep.execute(pattern=_SHARED_TOKEN, path=str(workspace / "id_rsa_old")))

    assert _SHARED_TOKEN in result and "PRIVATE-KEY" in result


def test_list_dir_hides_credential_files_and_directories(tmp_path) -> None:
    list_dir = _load_tool("list_dir")
    workspace = _make_secret_tree(tmp_path)

    result = asyncio.run(list_dir.execute(path=str(workspace), max_depth=5))

    assert "app.py" in result, "ordinary files must still be listed"
    assert "id_rsa_old" not in result
    assert ".ssh" not in result
    assert "prod.key" not in result


def test_glob_files_does_not_enumerate_secrets(tmp_path) -> None:
    glob_files = _load_tool("glob_files")
    workspace = _make_secret_tree(tmp_path)

    everything = asyncio.run(glob_files.execute(pattern="**/*", path=str(workspace)))
    explicit = asyncio.run(glob_files.execute(pattern="secrets/*", path=str(workspace)))
    ssh_probe = asyncio.run(glob_files.execute(pattern=".ssh/*", path=str(workspace)))

    assert "app.py" in everything
    assert "id_rsa_old" not in everything
    assert "prod.key" not in explicit, explicit
    assert "id_rsa" not in ssh_probe, ssh_probe
