"""Diff tool: diff_in_container — structured diff retrieval."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence

from docker.errors import NotFound

from sunaba.journal import record_tool_use
from sunaba.tools.common import _docker, _parse_numstat
from sunaba.tools.vcs import resolve_git_root

#: Path inside the container for clone/PR metadata (also referenced by
#: ``resolve_git_root`` in ``vcs.py`` and ``_write_clone_meta`` in
#: ``container.py``).
_META_PATH = "/home/sandbox/.sandbox-meta.json"


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
) -> str:
    """Show git diff between *base* and HEAD inside the container.

    Returns a structured JSON response with file-by-file summary
    (when *path* is omitted) or per-file hunks (when *path* is given).

    **Summary mode** (no *path*): returns a list of file records, each
    with ``path``, ``status``, ``additions``, ``deletions``, and
    ``changes`` count — the structured equivalent of
    ``git diff --numstat`` + ``git diff --name-status``.

    **File mode** (*path* given): returns the hunks for that file as an
    array of hunk objects with ``old_start``, ``old_count``,
    ``new_start``, ``new_count``, ``header``, and ``content`` (the hunk
    header + diff lines).  Supports *offset* / *limit* pagination.

    *raw* (``True``): include the full raw diff output as ``raw_diff``
    in the response so callers can always retrieve the complete text
    (escape-hatch principle).

    *base* defaults to the PR's base branch when the container was
    started with ``pr=N`` (stored in ``.sandbox-meta.json``), or
    ``HEAD~1`` when no base is recorded.  Pass an explicit ref
    (commit SHA, branch name, or relative ref like ``HEAD~3``) to
    override.

    Args:
        container_id: 12-character container ID prefix.
        base: Git ref to compare against.  ``None`` (default) reads
            ``base_branch`` from ``.sandbox-meta.json`` (set during
            ``pr=N`` checkout) or falls back to ``HEAD~1``.
        path: When given, return hunks for this file only.
        offset: 0-indexed line offset for hunks in file mode (default 0).
        limit: Maximum number of hunks to return in file mode (default 50).
        raw: When ``True``, include ``raw_diff`` field with the full
            raw ``git diff`` output (default ``False``).

    Returns:
        JSON string.  In summary mode:
        ``{files: [{path, status, additions, deletions, changes}, ...],
        total_files, total_additions, total_deletions[, raw_diff]}``.
        In file mode: ``{path: str, hunks: [...], shown, total, truncated,
        next_offset[, raw_diff]}``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(
        container_id[:12],
        "diff_in_container",
        {"base": base, "path": path},
    )

    working_dir = resolve_git_root(container)
    safe_wd = shlex.quote(working_dir)

    # Resolve base ref
    if base is None:
        meta = _read_container_meta(container)
        base = meta.get("base_branch", "")
    if not base:
        base = "HEAD~1"

    safe_base = shlex.quote(base)

    if path:
        return _file_diff(container, safe_wd, safe_base, path, offset, limit, raw_output=raw)
    return _summary_diff(container, safe_wd, safe_base, raw_output=raw)


def _run_diff(container, safe_wd: str, safe_base: str, extra_args: str = "") -> tuple[int, str]:
    """Run ``git diff`` and return (exit_code, stdout)."""
    cmd = f"cd {safe_wd} && git diff {safe_base}...HEAD {extra_args} 2>/dev/null"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""
    return ec, raw


def _summary_diff(container, safe_wd: str, safe_base: str, raw_output: bool = False) -> str:
    """Return file-by-file diff summary via ``--numstat`` + ``--name-status``."""
    # Run both --numstat and --name-status
    numstat_ec, numstat_raw = _run_diff(container, safe_wd, safe_base, "--numstat")
    name_status_ec, name_status_raw = _run_diff(container, safe_wd, safe_base, "--name-status")

    if numstat_ec != 0:
        return json.dumps({
            "status": "error",
            "error": f"git diff failed (exit {numstat_ec})",
            "raw_output": numstat_raw.strip(),
        })

    # Run raw diff if requested
    raw_diff_text: str | None = None
    if raw_output:
        raw_ec, raw_diff_text = _run_diff(container, safe_wd, safe_base, "")
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

    total_additions = sum(f.get("additions", 0) for f in files)
    total_deletions = sum(f.get("deletions", 0) for f in files)

    result: dict = {
        "files": files,
        "total_files": len(files),
        "total_additions": total_additions,
        "total_deletions": total_deletions,
    }

    if raw_output and raw_diff_text is not None:
        result["raw_diff"] = raw_diff_text

    return json.dumps(result)


def _file_diff(
    container, safe_wd: str, safe_base: str, path: str,
    offset: int, limit: int, raw_output: bool = False,
) -> str:
    """Return per-file hunks with pagination."""
    safe_path = shlex.quote(path)
    ec, raw = _run_diff(container, safe_wd, safe_base, f"-- {safe_path}")

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
