"""Shared helpers for code_sandbox_mcp tools."""

from __future__ import annotations

from typing import Any

#: Short per-request Docker API timeout (seconds) for *recovery* and
#: *poll* operations (e.g. ``sandbox_stop``, ``sandbox_exec_check``).
#:
#: When a container becomes unhealthy or its host is overloaded, an
#: untimed Docker API call can block indefinitely.  Because the MCP
#: client processes tool calls serially, one wedged call freezes the
#: whole session -- including the very tools needed to recover.  A short
#: timeout makes those calls fail fast instead (Issue #181).
RECOVERY_DOCKER_TIMEOUT: float = 15.0


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
        return docker.from_env(timeout=timeout)
    return docker.from_env()
