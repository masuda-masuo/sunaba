"""Checkpoint tools: checkpoint, checkpoint_list, checkpoint_restore."""

from __future__ import annotations

import json
import shlex

from docker.errors import NotFound

from sunaba.journal import record_boundary_crossing, record_tool_use
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.vcs.gitroot import resolve_git_root

# ---------------------------------------------------------------------------
# checkpoint -- local-only save point (no push, no verify, no token)
# ---------------------------------------------------------------------------


def checkpoint(
    container_id: str,
    message: str,
    working_dir: str | None = None,
) -> str:
    """Create a local Git checkpoint (commit only, no push).

    Container-local operation: no verify gate, no confirmation token,
    no network access required.  Use this frequently during edit/verify
    loops so you can roll back to any save point.

    Args:
        container_id: 12-character container ID prefix.
        message: Commit message for the checkpoint.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).

    Returns:
        JSON string with ``status``, ``sha`` (short), and ``message``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    safe_msg = shlex.quote(message)

    # Capture untracked files before git add -A sweeps them in
    ls_cmd = f"cd {safe_wd} && git ls-files --others --exclude-standard"
    ls_ec, ls_out = container.exec_run(
        ["/bin/sh", "-c", ls_cmd],
        stdout=True,
        stderr=True,
    )
    ls_stdout_b, _ = ls_out if isinstance(ls_out, tuple) else (ls_out, b"")
    ls_text = ls_stdout_b.decode("utf-8", errors="replace") if ls_stdout_b else ""
    swept_untracked = [f for f in ls_text.split("\n") if f.strip()]

    cmd = f"cd {safe_wd} && git add -A && git commit --allow-empty -m {safe_msg}"
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, stderr = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint",
            "error": stderr_text or stdout_text,
        })

    sha = ""
    sha_ec, sha_out = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_wd} && git rev-parse --short HEAD"],
        stdout=True,
    )
    if sha_ec == 0:
        sha_bytes = sha_out[0] if isinstance(sha_out, tuple) else sha_out
        sha = sha_bytes.decode("utf-8", errors="replace").strip() if sha_bytes else ""

    record_boundary_crossing(
        cid,
        "checkpoint",
        f"sha={sha} message={message[:80]}",
        approved=None,
    )

    return json.dumps({
        "status": "ok",
        "sha": sha,
        "message": message,
        "swept_untracked": swept_untracked,
    })


# ---------------------------------------------------------------------------
# checkpoint_list -- list local checkpoints
# ---------------------------------------------------------------------------


def checkpoint_list(
    container_id: str,
    working_dir: str | None = None,
    limit: int = 20,
) -> str:
    """List unpushed local Git checkpoints (no push, no verify, no token).

    Shows only commits that have not been pushed to any remote.  After
    :func:`publish` succeeds the list naturally becomes empty.

    Args:
        container_id: 12-character container ID prefix.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).
        limit: Maximum number of checkpoints to return (default 20).

    Returns:
        JSON string with ``checkpoints`` array, each entry with
        ``sha``, ``message``, and ``date``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(container_id[:12], "checkpoint_list")

    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    cmd = (
        f"cd {safe_wd} &&"
        f" git log --oneline --format='%h %aI %s' HEAD --not --remotes -{int(limit)}"
    )
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint_list",
            "error": stdout_text,
        })

    checkpoints = []
    for line in stdout_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) >= 3:
            checkpoints.append({
                "sha": parts[0],
                "date": parts[1],
                "message": parts[2],
            })
        elif len(parts) == 2:
            checkpoints.append({
                "sha": parts[0],
                "date": parts[1],
                "message": "",
            })

    return json.dumps({"checkpoints": checkpoints})


# ---------------------------------------------------------------------------
# checkpoint_restore -- rollback to a checkpoint
# ---------------------------------------------------------------------------


def checkpoint_restore(
    container_id: str,
    sha: str,
    working_dir: str | None = None,
) -> str:
    """Restore working tree to a previous checkpoint via ``git reset --hard``.

    **Warning:** This discards uncommitted changes.  Call
    :func:`checkpoint` first if you want to preserve current state.

    Only tracked files are restored -- untracked files are not removed.

    Container-local operation: no verify gate, no confirmation token.

    Args:
        container_id: 12-character container ID prefix.
        sha: SHA (or abbreviation) of the checkpoint to restore.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).

    Returns:
        JSON string with ``status``, ``restored_to``, and ``warning``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    safe_sha = shlex.quote(sha)

    cmd = f"cd {safe_wd} && git reset --hard {safe_sha}"
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, stderr = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint_restore",
            "error": stderr_text or stdout_text,
        })

    current_sha = ""
    sha_ec, sha_out = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_wd} && git rev-parse --short HEAD"],
        stdout=True,
    )
    if sha_ec == 0:
        sha_bytes = sha_out[0] if isinstance(sha_out, tuple) else sha_out
        current_sha = sha_bytes.decode("utf-8", errors="replace").strip() if sha_bytes else ""

    record_boundary_crossing(
        cid,
        "checkpoint_restore",
        f"restored_to={current_sha} requested={sha}",
        approved=None,
    )

    return json.dumps({
        "status": "ok",
        "restored_to": current_sha,
        "warning": (
            "Uncommitted changes were discarded. "
            "Checkpoints after the restored SHA are removed from git log "
            "(still in reflog). Untracked files are not cleaned."
        ),
    })
