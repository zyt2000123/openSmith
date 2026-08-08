"""Regression tests for the grep provider's secret-file exclusion.

``ToolGuard`` reasons about the path the model *names*.  ``grep`` names a
directory and returns the contents of every file underneath it, so a plain
directory search used to hand back the very files ``read_file`` is
CRITICAL-blocked from touching (``.env``, ``.ssh/id_rsa``, ``*.key``,
``.npmrc``).  The provider must therefore exclude secret-bearing files itself
when it walks a directory.

An explicitly named file is a different case: the guard sees that basename and
gates it, so a search the user pointed straight at a secret stays searchable
once approved.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from engine.safety.tool_guard import ToolGuard
from engine.tool.interface import ToolCall

ROOT = Path(__file__).resolve().parents[3]
RULES = ROOT / "agents" / "safety" / "dangerous_commands.json"

SECRET = "SYNTHETIC_SECRET_VALUE"


def _load_grep_provider():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agents.tools.grep")


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "project"
    (ws / ".ssh").mkdir(parents=True)
    (ws / "certs").mkdir()
    (ws / "backend").mkdir()
    (ws / ".env").write_text(f"STRIPE_KEY={SECRET}\n")
    (ws / "backend" / ".env").write_text(f"DB_PASSWORD={SECRET}\n")
    (ws / ".ssh" / "id_rsa").write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{SECRET}\n")
    (ws / "certs" / "server.key").write_text(f"-----BEGIN RSA PRIVATE KEY-----\n{SECRET}\n")
    (ws / "certs" / "chain.pem").write_text(f"-----BEGIN CERTIFICATE-----\n{SECRET}\n")
    (ws / ".npmrc").write_text(f"//registry.npmjs.org/:_authToken={SECRET}\n")
    (ws / ".pypirc").write_text(f"password = {SECRET}\n")
    # An ordinary file carrying the same needle, to prove the search still works.
    (ws / "app.py").write_text(f"# reference to {SECRET} in ordinary source\n")
    # Documented templates are not secrets and must stay searchable.
    (ws / ".env.example").write_text(f"STRIPE_KEY={SECRET}  # placeholder\n")
    return ws


def _grep(provider, **kwargs) -> str:
    return asyncio.run(provider.execute(**kwargs))


def test_directory_search_excludes_secret_files(tmp_path: Path) -> None:
    """A directory search must not return the contents of secret files."""
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws))

    leaked = [
        name
        for name in (".env", "id_rsa", "server.key", "chain.pem", ".npmrc", ".pypirc")
        if name in output
    ]
    assert not leaked, f"grep leaked secret files through a directory search: {leaked}"


def test_directory_search_still_finds_ordinary_files(tmp_path: Path) -> None:
    """Excluding secrets must not break the tool's actual job."""
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws))

    assert "app.py" in output


def test_env_template_is_searchable_when_named(tmp_path: Path) -> None:
    """Templates are swept up by the ``.env*`` exclusion but stay directly searchable.

    Keeping one exclusion glob rather than an exclude-then-reinclude pair keeps
    rg and grep -E on identical semantics; ripgrep supports a later rule
    overriding an earlier one, ``grep --exclude`` does not.  Nothing is lost:
    the guard treats a template basename as an ordinary read, so naming the
    file works.
    """
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    assert SECRET in _grep(provider, pattern=SECRET, path=str(ws / ".env.example"))


def test_nested_service_env_is_excluded(tmp_path: Path) -> None:
    """`<service>/.env` is the normal monorepo layout, not an edge case."""
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws / "backend"))

    assert ".env" not in output


def test_explicit_secret_file_stays_searchable(tmp_path: Path) -> None:
    """A file the model names is gated by the guard, so grep must not double-block it."""
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws / ".env"))

    assert SECRET in output, "an explicitly named, guard-gated file must stay searchable"


def test_guard_gates_an_explicitly_named_secret(tmp_path: Path) -> None:
    """Documents why the case above is safe: the guard sees that basename."""
    ws = _workspace(tmp_path)
    guard = ToolGuard(RULES, allowed_dirs=[ws])

    decision = guard.check(
        ToolCall(id="t", name="grep", arguments={"pattern": "x", "path": str(ws / ".env")}),
        audit=False,
    )

    assert decision.approval_required or not decision.allowed


@pytest.mark.parametrize("include", ["*", ".*", "*rc", "*.pem", "**/.env", "*.PEM"])
def test_include_filter_cannot_defeat_the_secret_exclusion(
    tmp_path: Path, include: str
) -> None:
    """A caller-supplied file filter must not re-include an excluded secret.

    ``--include`` beats ``--exclude`` in BSD/GNU grep regardless of flag order,
    and ripgrep gives precedence to the last matching glob — so an ``include``
    that can name a secret file turned the whole exclusion off and returned
    .env / *.pem / .npmrc contents from a plain directory search, on a tool that
    requires no approval at all.
    """
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws), include=include)

    assert output.startswith("Error:"), f"include={include!r} was not rejected"
    assert SECRET not in output


def test_ordinary_include_filter_still_works(tmp_path: Path) -> None:
    """The rejection must stay narrow: normal file-type filters keep working."""
    provider = _load_grep_provider()
    ws = _workspace(tmp_path)

    output = _grep(provider, pattern=SECRET, path=str(ws), include="*.py")

    assert "app.py" in output
    assert ".env" not in output


@pytest.mark.parametrize(
    "relative",
    [
        ".env",                    # control: already covered before this change
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".docker/config.json",
        ".config/gh/hosts.yml",
        ".config/gcloud/credentials.db",
    ],
)
def test_read_guard_gates_every_credential_store(tmp_path: Path, relative: str) -> None:
    """The read-side guard must cover the same names the Seatbelt profile denies.

    ``grep`` disables its own secret exclusion for a directly named file on the
    stated assumption that ToolGuard gates that basename.  For the credential
    configs the assumption did not hold: the profile denied ``cat ~/.npmrc``
    while ``read_file``/``grep`` returned the same npm token with no approval.

    ``FileGuard.check_path`` is exercised directly because ``ToolGuard.check``
    reaches it only once tool definitions are bound; without a registry it
    returns a generic "unknown tool" approval that would pass this assertion
    without ever consulting the sensitive-name rules.
    """
    target = tmp_path / "project" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"token={SECRET}\n")
    guard = ToolGuard(RULES, allowed_dirs=[tmp_path / "project"])

    result = guard.file_guard.check_path(str(target), writing=False)

    assert not result.allowed, f"{relative} was returned as a free read"
    assert result.approval_scope is not None and result.approval_scope.high_risk, (
        f"{relative} must need high-risk approval, not an ordinary boundary one "
        "(ELEVATED path approvals are cached by ToolRegistry)"
    )
