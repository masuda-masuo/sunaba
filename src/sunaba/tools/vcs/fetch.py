"""Shared git fetch helpers for VCS operations.

A single ``git fetch origin`` (no refspec) refreshes every remote-tracking
ref while leaving HEAD and the working tree untouched.  Tools that need to
resolve a ref that may not be present in the clone can call this instead of
crafting their own ``git fetch`` command.

See ``docs/design_branch_lifecycle.md`` for the design rationale.
"""

from __future__ import annotations

import logging
import shlex

logger = logging.getLogger(__name__)


def git_fetch_origin(container, working_dir: str) -> bool:
    """Run ``git fetch origin`` to refresh remote-tracking refs.

    Fetches all branches from origin, updating only remote-tracking refs
    (``origin/*``).  The working tree and HEAD are not modified.

    This is safe to call speculatively: if the network is unreachable or
    the remote has nothing new, the fetch is a no-op and returns False.

    Args:
        container: Docker container object with ``exec_run``.
        working_dir: Absolute path to the git repository root.

    Returns:
        True if the fetch completed (exit code 0), False on any failure.
    """
    safe_wd = shlex.quote(working_dir)
    ec, out = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_wd} && git fetch origin 2>&1"],
        stdout=True,
        stderr=True,
    )
    if ec == 0:
        logger.info("git fetch origin succeeded in %s", working_dir)
        return True
    _stdout, _stderr = (out if isinstance(out, tuple) else (out, b""))
    detail = (_stderr or _stdout).decode("utf-8", errors="replace").strip()
    logger.warning("git fetch origin failed in %s: %s", working_dir, detail)
    return False
