"""Shared helpers for code_sandbox_mcp tools."""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

#: Short per-request Docker API timeout (seconds) for *recovery* and
#: *poll* operations (e.g. ``sandbox_stop``, ``sandbox_exec_check``).
#:
#: A wedged/unhealthy container can make a Docker API call block up to
#: docker-py's ~60s default -- right around the MCP client's ~60s
#: timeout.  When a recovery/poll call crosses that client timeout the
#: stdio JSON-RPC stream can desync and wedge the *whole* session,
#: including Docker-independent tools such as ``sandbox_list_runs``
#: (see docs/issue-181-followup.md for the full diagnosis).  Bounding
#: these calls well under the client timeout keeps recovery answerable.
#:
#: Override via the ``CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT`` env var
#: (seconds); non-numeric or non-positive values fall back to the
#: 15s default (Issue #181).
_DEFAULT_RECOVERY_DOCKER_TIMEOUT: float = 15.0


def _recovery_timeout_from_env() -> float:
    """Resolve :data:`RECOVERY_DOCKER_TIMEOUT` from the environment.

    Reads ``CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT``; falls back to
    :data:`_DEFAULT_RECOVERY_DOCKER_TIMEOUT` for unset, non-numeric, or
    non-positive values.
    """
    raw = os.environ.get("CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT")
    if raw is None:
        return _DEFAULT_RECOVERY_DOCKER_TIMEOUT
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_RECOVERY_DOCKER_TIMEOUT
    return val if val > 0 else _DEFAULT_RECOVERY_DOCKER_TIMEOUT


RECOVERY_DOCKER_TIMEOUT: float = _recovery_timeout_from_env()


def _coerce_list_arg(v: object) -> object:
    """Coerce a JSON-stringified list to list (MCP client serialization workaround, issue #296)."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    return v


def _docker(timeout: float | None = None) -> Any:
    """Lazy-import docker and return a Docker client.

    Args:
        timeout: Per-request Docker API timeout in seconds.  ``None``
            (the default) uses docker-py's own default (60s).  Pass a
            short value (see :data:`RECOVERY_DOCKER_TIMEOUT`) for
            recovery / poll operations so a wedged container fails fast
            rather than hanging the whole MCP session (Issue #181).
    """
    import docker

    if timeout is not None:
        # docker-py types ``timeout`` as int, but seconds-as-float is
        # intentional here (sub-second recovery budgets); accepted at runtime.
        return docker.from_env(timeout=timeout)  # type: ignore[arg-type]
    return docker.from_env()


def _build_clone_command(
    repo: str,
    target: str,
    branch: str = "",
    authenticated: bool = False,
) -> str:
    """Build the in-container clone command, choosing transport by auth.

    *repo* must already be validated as ``owner/name`` by the caller
    (``_REPO_FORMAT_RE`` / ``_validate_clone_repo``), so interpolating it
    into the HTTPS URL is injection-safe.

    - **authenticated** (a VCS token is present, e.g. ``inject_vcs_token``
      or ``gh auth setup-git`` succeeded): use ``gh repo clone``, which
      authenticates via ``GH_TOKEN`` and so handles private *and* public
      repositories.
    - **anonymous** (no token): use a plain ``git clone`` over HTTPS.
      Public repos clone without credentials; ``GIT_TERMINAL_PROMPT=0``
      makes a *private* repo fail fast instead of hanging on an
      interactive credential prompt.  ``gh repo clone`` cannot be used
      here because ``gh`` requires authentication even for public repos
      (Issue #333).
    """
    safe_repo = shlex.quote(repo)
    safe_target = shlex.quote(target)
    if authenticated:
        cmd = f"gh repo clone {safe_repo} {safe_target}"
        if branch:
            cmd += f" -- -b {shlex.quote(branch)}"
        return cmd
    url = shlex.quote(f"https://github.com/{repo}.git")
    branch_opt = f"-b {shlex.quote(branch)} " if branch else ""
    return f"GIT_TERMINAL_PROMPT=0 git clone {branch_opt}{url} {safe_target}"
