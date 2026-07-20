"""Merge the remote base branch into the current branch inside the sandbox.

Authenticates via the egress proxy's read-authorization grant if the
container is proxied and holds no VCS token (#419), mirroring how
``_clone_repo_via_network`` and ``_setup_pr_branch`` fetch private repos.

Three tools are provided:

- ``merge_base`` — fetches the base branch and merges.  Returns clean
  merge status or the list of conflicted paths.
- ``merge_complete`` — after the operator has resolved conflicts via
  ``edit_file``, stages the resolutions and completes the in-progress merge.
- ``merge_abort`` — aborts an in-progress merge and returns the worktree
  to its pre-merge state.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
from typing import Any

from docker.errors import NotFound

from sunaba.journal import record_boundary_crossing
from sunaba.proxy_client import authorized_read_grant
from sunaba.tools.common import (
    META_PATH,
    _docker,
    container_not_found_error,
)
from sunaba.tools.github_api import _resolve_vcs_token
from sunaba.tools.vcs.gitroot import resolve_git_root

logger = logging.getLogger(__name__)


def _container_has_token(container) -> bool:
    """Check whether the container's environment carries a VCS token.

    Mirrors the logic in ``lifecycle.py``: ``container_has_token`` is
    ``True`` when the container was created with ``GITHUB_TOKEN`` or
    ``GH_TOKEN`` in its environment (#356/#439).  The container object's
    ``attrs['Config']['Env']`` is the creation-time environment, so this
    stays consistent with the decision made at container start.
    """
    env_list: list[str] = (
        container.attrs.get("Config", {}).get("Env", [])
        if hasattr(container, "attrs")
        else []
    )
    for env_entry in env_list:
        if env_entry.startswith("GITHUB_TOKEN=") or env_entry.startswith("GH_TOKEN="):
            return True
    return False


def _read_container_meta(container) -> dict:
    """Read ``.sandbox-meta.json`` from the container, or return empty dict."""
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         f"cat {shlex.quote(META_PATH)} 2>/dev/null || echo '{{}}'"],
        stdout=True,
    )
    if ec == 0:
        _stdout, _ = (out if isinstance(out, tuple) else (out, b""))
        raw = _stdout.decode("utf-8", errors="replace").strip() if _stdout else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _resolve_repo_from_container(container, working_dir: str) -> str:
    """Extract the ``owner/name`` from the container's ``origin`` remote.

    Runs ``git remote get-url origin`` and parses the GitHub HTTPS URL.
    Returns the ``"owner/name"`` string.

    Raises:
        RuntimeError: If the remote URL cannot be resolved or parsed.
    """
    safe_wd = shlex.quote(working_dir)
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         f"cd {safe_wd} && git remote get-url origin 2>/dev/null"],
        stdout=True,
        stderr=True,
    )
    stdout, stderr = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

    if ec != 0 or not stdout_text:
        raise RuntimeError(
            f"Cannot resolve origin remote URL: {stderr_text or stdout_text}"
        )

    # Parse https://github.com/owner/name.git -> owner/name
    # removesuffix() strips the literal ".git" suffix (unlike rstrip which
    # treats its argument as a character set, corrupting names that end in
    # those characters).  Trailing newlines and slashes are stripped first.
    url = stdout_text.strip().rstrip("/").removesuffix(".git")
    # Handle both https://github.com/owner/name and git@github.com:owner/name
    if "github.com/" in url:
        parts = url.split("github.com/", 1)[1]
    elif "github.com:" in url:
        parts = url.split("github.com:", 1)[1]
    else:
        raise RuntimeError(f"Unsupported remote URL format: {url}")

    # Validate owner/name
    if "/" in parts and len(parts.split("/")) == 2:
        return parts
    raise RuntimeError(f"Cannot parse owner/name from remote URL: {url}")


def _resolve_base_branch(
    container,
    working_dir: str,
    base_branch: str | None = None,
) -> tuple[str, str]:
    """Resolve the base branch to merge against.

    Resolution order:
    1. Caller-supplied *base_branch*.
    2. ``base_branch`` from container metadata (set by ``_setup_pr_branch``).
    3. Remote default branch (``origin/HEAD`` → symbolic ref).

    Returns ``(resolved_base, description)`` where *resolved_base* is the
    unadorned branch name (e.g. ``"main"``) and *description* says how it
    was determined.
    """
    if base_branch:
        return base_branch, f"explicit base branch: {base_branch}"

    # Check metadata
    meta = _read_container_meta(container)
    meta_base = meta.get("base_branch")
    if meta_base:
        return meta_base, f"base branch from container metadata: {meta_base}"

    # Fallback: resolve origin/HEAD to find the remote default branch
    safe_wd = shlex.quote(working_dir)
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         f"cd {safe_wd} && git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null"
         " || echo ''"],
        stdout=True,
        stderr=True,
    )
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""

    if stdout_text and stdout_text.startswith("refs/remotes/origin/"):
        default = stdout_text[len("refs/remotes/origin/"):]
        return default, f"remote default branch (origin/HEAD): {default}"

    # Last-resort: try well-known branch names
    for guess in ("main", "master"):
        ec2, out2 = container.exec_run(
            ["/bin/sh", "-c",
             f"cd {safe_wd} && git rev-parse --verify origin/{guess} 2>/dev/null"
             " || true"],
            stdout=True,
        )
        stdout2_text = out2.decode("utf-8", errors="replace").strip() if out2 else ""
        if stdout2_text:
            return guess, f"guessed base branch: {guess}"

    raise RuntimeError(
        "Cannot determine the base branch. "
        "Pass base_branch explicitly, ensure the container has metadata from "
        "a PR checkout, or make sure origin has a default branch."
    )


def merge_base(
    container_id: str,
    base_branch: str | None = None,
    repo: str | None = None,
    working_dir: str | None = None,
    *,
    _container: Any | None = None,
) -> str:
    """Fetch and merge the remote base branch into the current branch.

    On conflicts, resolve with ``edit_file`` then ``merge_complete``.
    Call ``merge_abort`` to abandon.

    Args:
        container_id: Container ID prefix.
        base_branch: Branch to merge.  Auto-resolved.
        repo: ``"owner/name"``.  Auto-resolved.
        working_dir: Git repo directory (auto-detect).

    Returns:
        JSON with result.
    """
    # Injection override for tests: skip Docker lookup entirely.
    if _container is not None:
        container = _container
    else:
        client = _docker()
        try:
            container = client.containers.get(container_id)
        except NotFound:
            return container_not_found_error(container_id)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    try:
        working_dir = resolve_git_root(container, working_dir)
    except Exception as e:
        return json.dumps({"status": "error", "step": "git_root", "error": str(e)})

    safe_wd = shlex.quote(working_dir)

    # Resolve repo (owner/name)
    if not repo:
        try:
            repo = _resolve_repo_from_container(container, working_dir)
        except RuntimeError as e:
            return json.dumps({"status": "error", "step": "repo_resolution", "error": str(e)})

    logger.info("merge_base: resolved repo=%s for container %s", repo, cid)

    # Resolve base branch
    try:
        resolved_base, base_description = _resolve_base_branch(
            container, working_dir, base_branch,
        )
    except RuntimeError as e:
        return json.dumps({"status": "error", "step": "base_resolution", "error": str(e)})

    logger.info("merge_base: %s for container %s", base_description, cid)

    # --- Fetch and merge with optional read grant ---
    # Mirror the logic from lifecycle.py:581: open_read_grant when the
    # container is proxied and holds no VCS token (#419).  A proxied
    # container is always token-free (#356), but we check both conditions
    # explicitly so the decision logic is auditable and matches clone.
    from sunaba.proxy_client import ProxyControlConfig
    proxy_cfg = ProxyControlConfig.from_env()
    proxied = proxy_cfg is not None
    has_token = _container_has_token(container)
    grant_open = proxied and not has_token

    anon_token: str | None = None
    if grant_open:
        try:
            anon_token = _resolve_vcs_token() or None
        except Exception:
            anon_token = None

    grant = (
        authorized_read_grant(repo, token=anon_token)
        if grant_open
        else contextlib.nullcontext()
    )
    journal_detail_base = (
        f"repo={repo} base={resolved_base} container={cid}"
        f" proxy_read_grant={grant_open}"
    )

    # Step 1: Fetch the base branch from origin
    safe_base = shlex.quote(resolved_base)
    fetch_cmd = (
        f"cd {safe_wd} && git fetch origin {safe_base} 2>&1"
    )

    try:
        with grant:
            fetch_ec, fetch_out = container.exec_run(
                ["/bin/sh", "-c", fetch_cmd],
                stdout=True,
                stderr=True,
            )
            f_stdout, f_stderr = (fetch_out if isinstance(fetch_out, tuple) else (fetch_out, b""))
            f_stdout_text = f_stdout.decode("utf-8", errors="replace") if f_stdout else ""
            f_stderr_text = f_stderr.decode("utf-8", errors="replace") if f_stderr else ""

            if fetch_ec != 0:
                detail = (f_stderr_text or f_stdout_text).strip()
                if grant_open:
                    record_boundary_crossing(
                        cid, "merge_base_fetch",
                        f"{journal_detail_base} step=fetch outcome=failed",
                        approved=False,
                    )
                return json.dumps({
                    "status": "error",
                    "step": "fetch",
                    "error": f"git fetch origin {resolved_base} failed (exit {fetch_ec}): {detail}",
                })

            logger.info(
                "merge_base: fetched origin/%s in container %s",
                resolved_base, cid,
            )

            # Step 2: Merge origin/<base_branch> into current branch
            merge_cmd = (
                f"cd {safe_wd}"
                f" && git merge origin/{safe_base} --no-edit 2>&1"
            )
            merge_ec, merge_out = container.exec_run(
                ["/bin/sh", "-c", merge_cmd],
                stdout=True,
                stderr=True,
            )
            m_stdout, m_stderr = (merge_out if isinstance(merge_out, tuple) else (merge_out, b""))
            m_stdout_text = m_stdout.decode("utf-8", errors="replace") if m_stdout else ""
            m_stderr_text = m_stderr.decode("utf-8", errors="replace") if m_stderr else ""
            merge_output = (m_stderr_text or m_stdout_text).strip()

            if merge_ec == 0:
                # Clean merge
                if grant_open:
                    record_boundary_crossing(
                        cid, "merge_base",
                        f"{journal_detail_base} step=merge outcome=clean",
                        approved=True,
                    )
                return json.dumps({
                    "status": "merged",
                    "base_branch": resolved_base,
                    "base_description": base_description,
                    "merge_output": merge_output,
                    "conflicts": [],
                    "message": (
                        f"Merged origin/{resolved_base} into current branch. "
                        "The merge commit is at HEAD. "
                        "Run publish(files=[...]) to push."
                    ),
                })

            # Step 3: Check if merge has conflicts (not just unrelated histories)
            # git merge exits 1 for conflicts, but can also fail for other reasons.
            # Check for "Merge conflict" or "CONFLICT" in output.
            if "CONFLICT" in merge_output or "conflict" in merge_output.lower():
                # Extract conflicted file paths
                # `git diff --name-only --diff-filter=U` lists unmerged paths
                conflict_cmd = (
                    f"cd {safe_wd}"
                    f" && git diff --name-only --diff-filter=U 2>/dev/null"
                )
                cf_ec, cf_out = container.exec_run(
                    ["/bin/sh", "-c", conflict_cmd],
                    stdout=True,
                )
                cf_stdout, _ = (cf_out if isinstance(cf_out, tuple) else (cf_out, b""))
                cf_text = cf_stdout.decode("utf-8", errors="replace") if cf_stdout else ""
                conflicted_files = [
                    f.strip() for f in cf_text.split("\n") if f.strip()
                ]

                if grant_open:
                    record_boundary_crossing(
                        cid, "merge_base",
                        f"{journal_detail_base} step=merge outcome=conflicts",
                        approved=False,
                    )

                return json.dumps({
                    "status": "conflicts",
                    "base_branch": resolved_base,
                    "base_description": base_description,
                    "merge_output": merge_output,
                    "conflicts": conflicted_files,
                    "message": (
                        f"Merging origin/{resolved_base} resulted in conflicts. "
                        "The worktree is in its conflicted state. "
                        "Resolve each conflicted file using edit_file, "
                        "then call merge_complete(container_id=...) "
                        "to stage all resolutions and complete the merge. "
                        "To abandon the merge, call merge_abort(container_id=...)."
                    ),
                })

            # Fallthrough: merge failed for a non-conflict reason (e.g.
            # unrelated histories without --allow-unrelated-histories)
            if grant_open:
                record_boundary_crossing(
                    cid, "merge_base",
                    f"{journal_detail_base} step=merge outcome=failed exit={merge_ec}",
                    approved=False,
                )
            return json.dumps({
                "status": "error",
                "step": "merge",
                "error": (
                    f"git merge origin/{resolved_base} failed (exit {merge_ec}):"
                    f" {merge_output}"
                ),
            })

    except Exception as e:
        if grant_open:
            record_boundary_crossing(
                cid, "merge_base",
                f"{journal_detail_base} step=exception error={e}",
                approved=False,
            )
        return json.dumps({
            "status": "error",
            "step": "merge",
            "error": f"Unexpected error during merge: {e}",
        })


def merge_complete(
    container_id: str,
    working_dir: str | None = None,
    *,
    _container: Any | None = None,
) -> str:
    """Stage the resolved conflicts and finish the merge commit.

    Stages only the paths the merge was waiting on, and refuses while any
    conflict marker remains.

    Args:
        container_id: Container ID prefix.
        working_dir: Git repo directory (auto-detect).

    Returns:
        JSON with result.
    """
    # Injection override for tests: skip Docker lookup entirely.
    if _container is not None:
        container = _container
    else:
        client = _docker()
        try:
            container = client.containers.get(container_id)
        except NotFound:
            return container_not_found_error(container_id)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    try:
        working_dir = resolve_git_root(container, working_dir)
    except Exception as e:
        return json.dumps({"status": "error", "step": "git_root", "error": str(e)})

    safe_wd = shlex.quote(working_dir)

    # Verify there is a merge in progress
    merge_head_cmd = (
        f"cd {safe_wd} && [ -f .git/MERGE_HEAD ] && echo 'in-progress' || echo 'none'"
    )
    mh_ec, mh_out = container.exec_run(
        ["/bin/sh", "-c", merge_head_cmd],
        stdout=True,
    )
    mh_stdout, _ = (mh_out if isinstance(mh_out, tuple) else (mh_out, b""))
    mh_text = mh_stdout.decode("utf-8", errors="replace").strip() if mh_stdout else ""

    if mh_text != "in-progress":
        return json.dumps({
            "status": "error",
            "step": "merge_check",
            "error": "No merge in progress. Call merge_base first.",
        })

    # Which paths is the merge still waiting on?  These must be read BEFORE
    # anything is staged: `git add` marks a conflicted path resolved whether or
    # not the operator actually resolved it, so a conflict check that runs
    # after staging can never find anything and would wave conflict markers
    # straight into the merge commit.
    unmerged_cmd = (
        f"cd {safe_wd} && git diff --name-only --diff-filter=U 2>/dev/null"
    )
    um_ec, um_out = container.exec_run(
        ["/bin/sh", "-c", unmerged_cmd],
        stdout=True,
    )
    um_stdout, _ = (um_out if isinstance(um_out, tuple) else (um_out, b""))
    um_text = um_stdout.decode("utf-8", errors="replace").strip() if um_stdout else ""
    unmerged = [f.strip() for f in um_text.split("\n") if f.strip()]

    # A leftover conflict marker means the file was never really resolved.
    if unmerged:
        marker_cmd = (
            f"cd {safe_wd} && grep -l -E '^(<<<<<<< |>>>>>>> |=======$)' -- "
            + " ".join(shlex.quote(p) for p in unmerged)
            + " 2>/dev/null || true"
        )
        mk_ec, mk_out = container.exec_run(
            ["/bin/sh", "-c", marker_cmd],
            stdout=True,
        )
        mk_stdout, _ = (mk_out if isinstance(mk_out, tuple) else (mk_out, b""))
        mk_text = mk_stdout.decode("utf-8", errors="replace").strip() if mk_stdout else ""
        unresolved = [f.strip() for f in mk_text.split("\n") if f.strip()]
        if unresolved:
            return json.dumps({
                "status": "error",
                "step": "resolve_check",
                "error": (
                    f"Conflict markers are still present in: {unresolved}. "
                    "Resolve them with edit_file, then call merge_complete again."
                ),
                "remaining_conflicts": unresolved,
            })

    # Stage exactly the paths the merge was waiting on -- never `git add -A`,
    # which would sweep in unrelated worktree edits the caller never declared
    # (the failure mode #677 removed from publish).  :(literal) keeps a path
    # containing glob characters from matching anything else.
    for path in unmerged:
        add_ec, add_out = container.exec_run(
            ["/bin/sh", "-c",
             f"cd {safe_wd} && git add -- "
             + shlex.quote(f":(literal){path}") + " 2>&1"],
            stdout=True,
            stderr=True,
        )
        if add_ec != 0:
            a_stdout, a_stderr = (
                add_out if isinstance(add_out, tuple) else (add_out, b"")
            )
            a_text = (a_stderr or a_stdout).decode("utf-8", errors="replace").strip()
            return json.dumps({
                "status": "error",
                "step": "git_add",
                "error": a_text,
                "path": path,
            })

    # Complete the merge
    commit_cmd = f"cd {safe_wd} && git commit --no-edit 2>&1"
    cm_ec, cm_out = container.exec_run(
        ["/bin/sh", "-c", commit_cmd],
        stdout=True,
        stderr=True,
    )
    cm_stdout, cm_stderr = (cm_out if isinstance(cm_out, tuple) else (cm_out, b""))
    cm_out_text = cm_stdout.decode("utf-8", errors="replace") if cm_stdout else ""
    cm_err_text = cm_stderr.decode("utf-8", errors="replace") if cm_stderr else ""

    if cm_ec != 0:
        return json.dumps({
            "status": "error",
            "step": "merge_commit",
            "error": (cm_err_text or cm_out_text).strip(),
        })

    # Get short SHA
    sha_cmd = f"cd {safe_wd} && git rev-parse --short HEAD"
    sh_ec, sh_out = container.exec_run(
        ["/bin/sh", "-c", sha_cmd],
        stdout=True,
    )
    sh_stdout, _ = (sh_out if isinstance(sh_out, tuple) else (sh_out, b""))
    sha = sh_stdout.decode("utf-8", errors="replace").strip() if sh_stdout else ""

    record_boundary_crossing(
        cid,
        "merge_complete",
        f"container={cid} sha={sha}",
        approved=True,
    )

    return json.dumps({
        "status": "completed",
        "sha": sha,
        "message": (
            "Merge completed successfully. "
            f"HEAD is now {sha}. "
            "Run publish(files=[...]) to push."
        ),
    })


def merge_abort(
    container_id: str,
    working_dir: str | None = None,
    *,
    _container: Any | None = None,
) -> str:
    """Abort an in-progress merge (``git merge --abort``).

    Restores the worktree to its pre-merge state.

    Args:
        container_id: Container ID prefix.
        working_dir: Git repo directory (auto-detect).

    Returns:
        JSON with result.
    """
    # Injection override for tests: skip Docker lookup entirely.
    if _container is not None:
        container = _container
    else:
        client = _docker()
        try:
            container = client.containers.get(container_id)
        except NotFound:
            return container_not_found_error(container_id)
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    try:
        working_dir = resolve_git_root(container, working_dir)
    except Exception as e:
        return json.dumps({"status": "error", "step": "git_root", "error": str(e)})

    safe_wd = shlex.quote(working_dir)

    # Verify there is a merge in progress
    merge_head_cmd = (
        f"cd {safe_wd} && [ -f .git/MERGE_HEAD ] && echo 'in-progress' || echo 'none'"
    )
    mh_ec, mh_out = container.exec_run(
        ["/bin/sh", "-c", merge_head_cmd],
        stdout=True,
    )
    mh_stdout, _ = (mh_out if isinstance(mh_out, tuple) else (mh_out, b""))
    mh_text = mh_stdout.decode("utf-8", errors="replace").strip() if mh_stdout else ""

    if mh_text != "in-progress":
        return json.dumps({
            "status": "error",
            "step": "merge_check",
            "error": "No merge in progress. There is nothing to abort.",
        })

    # Abort the merge
    abort_cmd = f"cd {safe_wd} && git merge --abort 2>&1"
    ab_ec, ab_out = container.exec_run(
        ["/bin/sh", "-c", abort_cmd],
        stdout=True,
        stderr=True,
    )
    ab_stdout, ab_stderr = (ab_out if isinstance(ab_out, tuple) else (ab_out, b""))
    ab_out_text = ab_stdout.decode("utf-8", errors="replace") if ab_stdout else ""
    ab_err_text = ab_stderr.decode("utf-8", errors="replace") if ab_stderr else ""

    if ab_ec != 0:
        return json.dumps({
            "status": "error",
            "step": "merge_abort",
            "error": (ab_err_text or ab_out_text).strip(),
        })

    record_boundary_crossing(
        cid,
        "merge_abort",
        f"container={cid}",
        approved=False,
    )

    return json.dumps({
        "status": "aborted",
        "message": (
            "Merge aborted. The worktree has been returned to its "
            "pre-merge state. You can start a new merge with "
            "merge_base(container_id=...)."
        ),
    })
