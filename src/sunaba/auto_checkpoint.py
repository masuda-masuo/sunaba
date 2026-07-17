"""Auto-checkpoint after edit operations (Issue #586).

Creates automatic git checkpoints after successful edit operations
so that changes are never lost even if publish fails.  The counter
is process-local in-memory (same pattern as verify_state.py) and
resets on server restart.
"""

from __future__ import annotations

import json
import logging
import shlex
import threading

logger = logging.getLogger(__name__)

#: Process-local counter: container_id[:12] -> count of auto-checkpoints.
#: Resets on server restart (process-local, not persisted).
_auto_checkpoint_counter: dict[str, int] = {}
_lock: threading.Lock = threading.Lock()


def _git(container, working_dir: str, cmd: str) -> tuple[int, str, str]:
    """Run a command in the container's git working directory."""
    safe_wd = shlex.quote(working_dir)
    full_cmd = f"cd {safe_wd} && {cmd}"
    ec, out = container.exec_run(
        ["/bin/sh", "-c", full_cmd],
        stdout=True,
        stderr=True,
    )
    stdout_bytes, stderr_bytes = (out if isinstance(out, tuple) else (out, b""))
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return ec, stdout, stderr


def get_changed_files(container, working_dir: str) -> list[str]:
    """Return list of changed (modified + untracked) files from git status.

    Returns an empty list on any git error (no-op).
    """
    ec, out, _ = _git(container, working_dir, "git status --porcelain")
    if ec != 0:
        return []
    files: list[str] = []
    for line in out.split("\n"):
        line = line.rstrip("\r\n")
        if not line:
            continue
        # Format: "XY filename" — XY are two status chars (may include
        # spaces, e.g. " M" for unstaged modification), then a separator
        # space, then the filename.  Never strip() the line: that would
        # damage the XY field when X is a space.
        if len(line) > 3:
            files.append(line[3:])
    return files


def auto_checkpoint(
    container,
    container_id: str,
    working_dir: str | None = None,
) -> str | None:
    """Create an auto-checkpoint after a successful edit operation.

    No-op (returns ``None``) when *resolve_git_root* fails (outside a git
    area) or when there are no uncommitted changes.

    Returns a JSON string with ``status``, ``sha``, and ``message`` on
    success.
    """
    # Lazy import to avoid circular dependency (file.py/vcs.py import
    # auto_checkpoint, which needs resolve_git_root from vcs.py).
    from sunaba.tools.vcs import resolve_git_root  # noqa: PLC0415

    try:
        root = resolve_git_root(container, working_dir)
    except Exception:
        logger.debug("auto_checkpoint: resolve_git_root failed, skipping")
        return None

    changed = get_changed_files(container, root)
    if not changed:
        logger.debug("auto_checkpoint: no changes to commit, skipping")
        return None

    changed_str = ", ".join(changed)
    message = f"[auto] checkpoint \u2014 {changed_str}"
    safe_msg = shlex.quote(message)

    ec, out, err = _git(
        container, root,
        f"git add -A && git commit --allow-empty -m {safe_msg}",
    )
    if ec != 0:
        logger.warning("auto_checkpoint: git commit failed: %s", err or out)
        return None

    # Retrieve the short SHA.
    sha_ec, sha_out, _ = _git(container, root, "git rev-parse --short HEAD")
    sha = sha_out.strip() if sha_ec == 0 else ""

    # Increment the process-local counter.
    cid = container_id[:12]
    with _lock:
        _auto_checkpoint_counter[cid] = _auto_checkpoint_counter.get(cid, 0) + 1

    logger.info("Auto-checkpoint created: %s", message)
    return json.dumps({
        "status": "ok",
        "sha": sha,
        "message": message,
    })


def increment_counter(container_id: str) -> None:
    """Increment the auto-checkpoint counter for *container_id*.

    Used by the explicit :func:`checkpoint` tool so that explicit
    checkpoints are counted alongside auto-generated ones.
    """
    cid = container_id[:12]
    with _lock:
        _auto_checkpoint_counter[cid] = _auto_checkpoint_counter.get(cid, 0) + 1


def counter_for(container_id: str) -> int:
    """Return the current auto-checkpoint count for *container_id*.

    Exposed for testing and observability.
    """
    with _lock:
        return _auto_checkpoint_counter.get(container_id[:12], 0)
