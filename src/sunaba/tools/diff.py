"""Diff tool: diff_in_container — structured diff retrieval."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence

from docker.errors import NotFound

from sunaba.journal import record_tool_use
from sunaba.tools.common import (
    META_PATH,
    _docker,
    _parse_numstat,
    container_not_found_error,
)
from sunaba.tools.vcs import resolve_git_root
from sunaba.tools.vcs.merge_base import _resolve_base_branch

#: Path inside the container for clone/PR metadata (also referenced by
#: ``resolve_git_root`` in ``vcs.py`` and ``_write_clone_meta`` in
#: ``container.py``).
_META_PATH = META_PATH


def _read_container_meta(container) -> dict:
    """Read ``.sandbox-meta.json`` from the container, or return empty dict."""
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         f"cat {shlex.quote(_META_PATH)} 2>/dev/null || echo '{{}}'"],
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


def _parse_name_status(lines: Sequence[str]) -> dict[str, str]:
    """Parse ``git diff --name-status`` output into a {path: status} mapping.

    Format::

        <status><tab><path>
        R<similarity><tab><old_path><tab><new_path>

    Returns a dict mapping the **current** path to its status character
    (M=Modified, A=Added, D=Deleted, R=Renamed, C=Copied).
    For renamed files the new (current) path is used as key.
    """
    status_map: dict[str, str] = {}
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        raw_status = parts[0]
        status = raw_status[0] if raw_status else ""
        if status in ("R", "C") and len(parts) >= 3:
            status_map[parts[2]] = status
        elif len(parts) >= 2:
            status_map[parts[1]] = status
    return status_map


def diff_in_container(
    container_id: str,
    base: str | None = None,
    path: str | None = None,
    offset: int = 0,
    limit: int = 50,
    raw: bool = False,
    worktree: bool = False,
) -> str:
    """Show git diff: changed-file summary, or hunks for one path.

    Covers committed and uncommitted work since *base* diverged from HEAD.
    worktree=True: uncommitted only.  Untracked paths listed separately;
    response echoes the base and mode used.

    Args:
        container_id: Container ID prefix.
        base: Ref to diff against; default: PR base, else repo default
            branch.  Ignored when worktree=True.
        path: Return hunks for this file only.
        offset: Hunk paging offset (0-indexed).
        limit: Max hunks per page.
        raw: Also include the complete raw diff as raw_diff.
        worktree: Show only uncommitted changes (vs HEAD); ignores base.

    Returns:
        JSON.  Summary: files, total_files, total_additions,
        total_deletions, untracked, base, mode.  File mode: path,
        hunks, shown, total, truncated, next_offset, base, mode.
        Plus raw_diff when raw=True.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(
        container_id[:12],
        "diff_in_container",
        {"base": base, "path": path, "worktree": worktree},
    )

    working_dir = resolve_git_root(container)
    safe_wd = shlex.quote(working_dir)

    # Resolve base ref.  An unresolvable base is an error, never a silent
    # fallback: degrading to "no base" is how a caller ends up reviewing a
    # plausible-looking diff of something other than their own work (#748).
    if worktree:
        actual_base = "HEAD"
        mode = "worktree"
        safe_base = shlex.quote(actual_base)
    else:
        mode = "merge-base"
        if base is not None:
            actual_base = base
        else:
            meta = _read_container_meta(container)
            actual_base = meta.get("base_branch", "")
            if not actual_base:
                try:
                    actual_base, _ = _resolve_base_branch(container, working_dir)
                except RuntimeError as exc:
                    return json.dumps({
                        "status": "error",
                        "step": "resolve_base",
                        "error": (
                            f"Cannot determine the base to diff against: {exc} "
                            "Pass base= explicitly."
                        ),
                    })
            # An auto-resolved base is a branch name, which git reads as the
            # local branch.  That branch moves with the container's own
            # commits, so prefer the remote-tracking ref (see the helper).
            # An explicitly passed base is left exactly as the caller wrote it.
            actual_base = _prefer_remote_tracking(
                container, safe_wd, actual_base
            )

        # Resolve the merge base to a concrete commit here rather than inside
        # a shell command substitution.  `git diff $(git merge-base ...)` with
        # a failing substitution silently becomes a bare `git diff`, which
        # exits 0 and reports only unstaged changes -- a wrong answer
        # indistinguishable from a correct one.
        merge_base_sha = _resolve_merge_base(container, safe_wd, actual_base)
        if merge_base_sha is None:
            return json.dumps({
                "status": "error",
                "step": "merge_base",
                "error": (
                    f"No merge base between {actual_base!r} and HEAD. "
                    "The ref may not exist in this clone (branches created "
                    "after the container started are not fetched), or it "
                    "shares no history with HEAD."
                ),
            })
        safe_base = shlex.quote(merge_base_sha)

    if path:
        result = json.loads(
            _file_diff(
                container, safe_wd, safe_base, path,
                offset, limit, raw_output=raw, worktree=worktree,
            )
        )
        if "status" not in result or result["status"] != "error":
            result["base"] = actual_base
            result["mode"] = mode
        return json.dumps(result)

    result = json.loads(
        _summary_diff(
            container, safe_wd, safe_base, raw_output=raw, worktree=worktree,
        )
    )
    if "status" not in result or result["status"] != "error":
        result["base"] = actual_base
        result["mode"] = mode
    return json.dumps(result)


def _prefer_remote_tracking(container, safe_wd: str, branch: str) -> str:
    """Return ``origin/<branch>`` when that ref exists, else *branch*.

    An auto-resolved base is a plain branch name, which git reads as the
    *local* branch.  A sandbox container works directly on the cloned default
    branch, so after a ``checkpoint`` the local branch has moved along with
    HEAD and ``merge-base(main, HEAD)`` collapses to HEAD -- committed work
    silently vanishes from the diff.  The remote-tracking ref does not move,
    so it is the honest answer to "where did my work start".
    """
    if branch.startswith("origin/"):
        return branch
    cmd = (
        f"cd {safe_wd} && git rev-parse --verify --quiet "
        f"{shlex.quote('origin/' + branch)}"
    )
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    resolved = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    return f"origin/{branch}" if ec == 0 and resolved else branch


def _resolve_merge_base(container, safe_wd: str, base: str) -> str | None:
    """Return the merge-base commit of *base* and HEAD, or ``None``.

    ``None`` means git could not produce one -- an unknown ref, or two
    histories with no common ancestor.  The caller must treat that as an
    error rather than diffing against nothing.
    """
    cmd = f"cd {safe_wd} && git merge-base {shlex.quote(base)} HEAD"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    sha = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    if ec != 0 or not sha:
        return None
    return sha.splitlines()[0].strip()


def _run_diff(
    container, safe_wd: str, safe_base: str, extra_args: str = "",
    worktree: bool = False,
) -> tuple[int, str]:
    """Run ``git diff`` and return (exit_code, stdout)."""
    if worktree:
        # Compare HEAD tree against working tree (2-way, not triple-dot)
        cmd = f"cd {safe_wd} && git diff HEAD {extra_args} 2>/dev/null"
    else:
        # safe_base is already the resolved merge-base commit (see
        # diff_in_container).  Diffing it against the working tree shows
        # committed work plus uncommitted changes, without mutating the index.
        cmd = f"cd {safe_wd} && git diff {safe_base} {extra_args} 2>/dev/null"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""
    return ec, raw


def _summary_diff(
    container, safe_wd: str, safe_base: str, raw_output: bool = False,
    worktree: bool = False,
) -> str:
    """Return file-by-file diff summary via ``--numstat`` + ``--name-status``."""
    # Run both --numstat and --name-status
    numstat_ec, numstat_raw = _run_diff(
        container, safe_wd, safe_base, "--numstat", worktree=worktree,
    )
    name_status_ec, name_status_raw = _run_diff(
        container, safe_wd, safe_base, "--name-status", worktree=worktree,
    )

    if numstat_ec != 0:
        return json.dumps({
            "status": "error",
            "error": f"git diff failed (exit {numstat_ec})",
            "raw_output": numstat_raw.strip(),
        })

    # Run raw diff if requested
    raw_diff_text: str | None = None
    if raw_output:
        raw_ec, raw_diff_text = _run_diff(
            container, safe_wd, safe_base, "", worktree=worktree,
        )
        if raw_ec != 0:
            return json.dumps({
                "status": "error",
                "error": f"git diff failed (exit {raw_ec})",
                "raw_output": raw_diff_text.strip(),
            })

    # Parse name-status to get status per path
    name_status_lines = name_status_raw.split("\n")
    status_map = _parse_name_status(name_status_lines)

    # Parse numstat for additions/deletions
    numstat_lines = numstat_raw.split("\n")
    files = _parse_numstat(numstat_lines)

    # Merge status into each file record
    name_status_failed = name_status_ec != 0
    for f in files:
        p = f.get("path", "")
        if p in status_map:
            f["status"] = status_map[p]
        elif name_status_failed:
            f["status"] = "?"  # --name-status failed
        else:
            f["status"] = "M"  # default: modified

    # Include untracked files (always, not just in worktree mode)
    untracked = _get_untracked_files(container, safe_wd)
    files.extend(untracked)
    untracked_list = [f["path"] for f in untracked]

    total_additions = sum(f.get("additions", 0) for f in files)
    total_deletions = sum(f.get("deletions", 0) for f in files)

    result: dict = {
        "files": files,
        "total_files": len(files),
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "untracked": untracked_list,
    }

    if raw_output and raw_diff_text is not None:
        result["raw_diff"] = raw_diff_text

    return json.dumps(result)


def _get_untracked_files(container, safe_wd: str) -> list[dict]:
    """Get untracked files with line counts via ``git ls-files``."""
    cmd = (
        f"cd {safe_wd} && git ls-files --others --exclude-standard"
        " | xargs -r -I{} wc -l {} 2>/dev/null"
    )
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""
    result: list[dict] = []
    for line in raw.strip().split("\n"):
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            count = int(parts[0])
        except ValueError:
            continue
        fpath = parts[1]
        result.append({
            "path": fpath,
            "status": "untracked",
            "additions": count,
            "deletions": 0,
            "changes": count,
        })
    return result


def _is_untracked(container, safe_wd: str, path: str) -> bool:
    """Check if a path is untracked."""
    safe_path = shlex.quote(path)
    cmd = f"cd {safe_wd} && git ls-files --others --exclude-standard -- {safe_path} 2>/dev/null"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    return bool(raw)


def _parse_hunks(raw: str) -> list[dict]:
    """Parse ``git diff`` output into structured hunk objects."""
    hunks: list[dict] = []
    current_hunk: dict | None = None
    for line in raw.split("\n"):
        hunk_match = re.match(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
        if hunk_match:
            if current_hunk:
                hunks.append(current_hunk)
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
            current_hunk = {
                "old_start": int(hunk_match.group(1)),
                "old_count": old_count,
                "new_start": int(hunk_match.group(3)),
                "new_count": new_count,
                "header": line,
                "content": line + "\n",
            }
        elif current_hunk is not None:
            # ``\ No newline at end of file`` is part of the previous hunk
            current_hunk["content"] += line + "\n"
    if current_hunk:
        hunks.append(current_hunk)
    return hunks


def _untracked_file_diff(
    container, safe_wd: str, path: str, offset: int, limit: int,
    raw_output: bool = False,
) -> str:
    """Return hunks for an untracked file via ``git diff --no-index``."""
    safe_path = shlex.quote(path)
    cmd = f"cd {safe_wd} && git diff --no-index -- /dev/null {safe_path} 2>/dev/null"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""

    # --no-index exit code 1 means files differ (normal for this comparison)
    if ec not in (0, 1):
        return json.dumps({
            "status": "error",
            "error": f"git diff failed (exit {ec})",
            "raw_output": raw.strip(),
        })

    if not raw.strip():
        return json.dumps({
            "status": "error",
            "error": f"No diff for path: {path}",
        })

    hunks = _parse_hunks(raw)

    total = len(hunks)
    truncated = (offset + limit) < total
    page = hunks[offset:offset + limit]
    next_offset = offset + limit if truncated else None

    result: dict = {
        "path": path,
        "hunks": page,
        "shown": len(page),
        "total": total,
        "truncated": truncated,
        "next_offset": next_offset,
    }

    if raw_output:
        result["raw_diff"] = raw

    return json.dumps(result)


def _file_diff(
    container, safe_wd: str, safe_base: str, path: str,
    offset: int, limit: int, raw_output: bool = False,
    worktree: bool = False,
) -> str:
    """Return per-file hunks with pagination."""
    safe_path = shlex.quote(path)
    ec, raw = _run_diff(
        container, safe_wd, safe_base, f"-- {safe_path}", worktree=worktree,
    )

    # An untracked file won't appear in git diff, fall back to --no-index
    if ec != 0 or not raw.strip():
        if _is_untracked(container, safe_wd, path):
            return _untracked_file_diff(
                container, safe_wd, path, offset, limit, raw_output,
            )

    if ec != 0:
        return json.dumps({
            "status": "error",
            "error": f"git diff failed (exit {ec})",
            "raw_output": raw.strip(),
        })

    if not raw.strip():
        return json.dumps({
            "status": "error",
            "error": f"No diff for path: {path}",
        })

    hunks = _parse_hunks(raw)

    total = len(hunks)
    truncated = (offset + limit) < total
    page = hunks[offset:offset + limit]
    next_offset = offset + limit if truncated else None

    result: dict = {
        "path": path,
        "hunks": page,
        "shown": len(page),
        "total": total,
        "truncated": truncated,
        "next_offset": next_offset,
    }

    if raw_output:
        result["raw_diff"] = raw

    return json.dumps(result)
