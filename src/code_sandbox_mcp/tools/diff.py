"""Diff tool: diff_in_container — structured diff retrieval."""

from __future__ import annotations

import json
import re
import shlex
from typing import Sequence

from docker.errors import NotFound

from code_sandbox_mcp.journal import record_tool_use
from code_sandbox_mcp.tools.common import _docker
from code_sandbox_mcp.tools.vcs import resolve_git_root

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


def diff_in_container(
    container_id: str,
    base: str | None = None,
    path: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Show git diff between *base* and HEAD inside the container.

    Returns a structured JSON response with file-by-file summary
    (when *path* is omitted) or per-file hunks (when *path* is given).

    **Summary mode** (no *path*): returns a list of file records, each
    with ``path``, ``additions``, ``deletions``, and ``changes`` count
    \u2014 the structured equivalent of ``git diff --numstat``.

    **File mode** (*path* given): returns the hunks for that file as an
    array of hunk objects with ``old_start``, ``old_count``,
    ``new_start``, ``new_count``, ``header``, and ``content`` (the hunk
    header + diff lines).  Supports *offset* / *limit* pagination.

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

    Returns:
        JSON string.  In summary mode:
        ``{files: [...], total_files, total_additions, total_deletions}``.
        In file mode: ``{path: str, hunks: [...], shown, total, truncated,
        next_offset}``.
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
        return _file_diff(container, safe_wd, safe_base, path, offset, limit)
    return _summary_diff(container, safe_wd, safe_base)


def _summary_diff(container, safe_wd: str, safe_base: str) -> str:
    """Return file-by-file diff summary via ``--numstat``."""
    cmd = f"cd {safe_wd} && git diff {safe_base}...HEAD --numstat 2>/dev/null"
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "error": f"git diff failed (exit {ec})",
            "raw_output": raw.strip(),
        })

    lines = raw.split("\n")
    files = _parse_numstat(lines)
    total_additions = sum(f.get("additions", 0) for f in files)
    total_deletions = sum(f.get("deletions", 0) for f in files)

    return json.dumps({
        "files": files,
        "total_files": len(files),
        "total_additions": total_additions,
        "total_deletions": total_deletions,
    })


def _file_diff(container, safe_wd: str, safe_base: str, path: str, offset: int, limit: int) -> str:
    """Return per-file hunks with pagination."""
    safe_path = shlex.quote(path)
    cmd = (
        f"cd {safe_wd} && git diff {safe_base}...HEAD -- {safe_path} 2>/dev/null"
    )
    ec, out = container.exec_run(["/bin/sh", "-c", cmd], stdout=True)
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    raw = stdout.decode("utf-8", errors="replace") if stdout else ""

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

    return json.dumps({
        "path": path,
        "hunks": page,
        "shown": len(page),
        "total": total,
        "truncated": truncated,
        "next_offset": next_offset,
    })
