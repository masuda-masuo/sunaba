"""Shared helpers for sunaba tools."""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Sequence

#: The container's workspace: the git repository root.
#:
#: Containers are created with this as their working directory, so an
#: ``exec_run`` that names no ``workdir`` still runs inside the repo.  That is
#: what makes the repo root unambiguous -- see
#: ``docs/design_filesystem_layout.md`` and Issue #600, where runners that
#: forgot to pass ``workdir`` silently ran in the home directory instead.
WORKSPACE = "/workspace"

#: Working directory of containers created before the workspace became the
#: repo root.  Their repo lives elsewhere (``/tmp/repo/*``, ``/home/sandbox``),
#: so :func:`sunaba.tools.vcs.resolve_git_root` still has to probe for it.
LEGACY_WORKDIR = "/home/sandbox"

#: Container metadata written by ``sandbox_initialize`` after a clone.  It
#: stays in the home directory on purpose: inside the workspace it would show
#: up in ``git status``.
META_PATH = f"{LEGACY_WORKDIR}/.sandbox-meta.json"


def _parse_numstat(lines: Sequence[str]) -> list[dict]:
    """Parse ``git diff --numstat`` output into structured records.

    Format (tab-separated)::

        additions<tab>deletions<tab>path
        -<tab>-<tab>path   (binary)

    Example::

        10      5       src/foo.py
        3       1       src/bar.py
    """
    records: list[dict] = []
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        raw_add, raw_del, path = parts[0], parts[1], parts[2]
        if raw_add == "-" and raw_del == "-":
            records.append({
                "path": path,
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "binary": True,
            })
        else:
            try:
                additions = int(raw_add)
                deletions = int(raw_del)
            except ValueError:
                continue
            records.append({
                "path": path,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
            })
    return records


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
#: Override via the ``SUNABA_RECOVERY_DOCKER_TIMEOUT`` env var
#: (seconds); non-numeric or non-positive values fall back to the
#: 15s default (Issue #181).
_DEFAULT_RECOVERY_DOCKER_TIMEOUT: float = 15.0


def _recovery_timeout_from_env() -> float:
    """Resolve :data:`RECOVERY_DOCKER_TIMEOUT` from the environment.

    Reads ``SUNABA_RECOVERY_DOCKER_TIMEOUT``; falls back to
    :data:`_DEFAULT_RECOVERY_DOCKER_TIMEOUT` for unset, non-numeric, or
    non-positive values.
    """
    raw = os.environ.get("SUNABA_RECOVERY_DOCKER_TIMEOUT")
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


#: Nudge attached to "container not found" errors (Issue #550): for an
#: agent operating without instruction files, the error response is the
#: only channel that can point to the right first move, so the shared
#: payload carries a ``recommended_next_action`` field.  Advisory only.
CONTAINER_NOT_FOUND_NEXT_ACTION = (
    "sandbox_list_containers to find running containers, "
    "or sandbox_initialize to start one"
)


def container_not_found_error(container_id: str, **extra: Any) -> str:
    """Return the shared JSON error payload for a missing container.

    Carries a ``recommended_next_action`` nudge (advisory, Issue #550).
    *extra* fields are merged into the payload so callers can keep
    tool-specific keys (e.g. ``gate_passed=False`` for verify).
    """
    payload: dict[str, Any] = {
        "status": "error",
        "error": f"Container {container_id[:12]} not found",
        "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
    }
    payload.update(extra)
    return json.dumps(payload)


#: Warning appended to a clone result when the repo was cloned *without* a
#: VCS token.  The clone itself is anonymous (public repos only, read-only
#: working tree), but ``publish`` no longer requires the token to have been
#: present at container start: it lazily injects a host-resolved token into
#: the push exec (Issue #347), so a no-token clone can still be published
#: afterward without a re-init.  Surfacing this at clone time only flags that
#: the *clone* was unauthenticated (a private repo would have failed), not
#: that pushing is impossible.
CLONE_NO_TOKEN_WARNING = (
    "cloned without a VCS token (anonymous clone; private repos would fail). "
    "publish can still push later: it injects a host-resolved token into the "
    "push step on demand (no re-init needed), provided the host has a token "
    "available (GITHUB_TOKEN / broker)."
)


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

    - **authenticated** (a VCS token is present, e.g. ``gh auth setup-git``
      succeeded): use ``gh repo clone``, which
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
