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


def _read_container_meta(container) -> dict:
    """Read ``.sandbox-meta.json`` from the container, or return empty dict."""
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         "cat /home/sandbox/.sandbox-meta.json 2>/dev/null || echo '{}'"],
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


def _parse_diffstat(lines: Sequence[str]) -> list[dict]:
    """Parse ``git diff --stat`` output into structured records.

    Handles the standard format::

        src/foo.py | 10 +++++-----
        src/bar.py | 3 ++-
        2 files changed, 8 insertions(+), 5 deletions(-)
    """
    records: list[dict] = []
    for line in lines:
        line = line.rstrip()
        if not line or " changed," in line or " file" in line:
            continue
        m = re.match(
            r"^\s*(.+?)\s+\|\s+(\d+)\s+([+-]+)$",
            line,
        )
        if m:
            path_str = m.group(1).strip()
            changes = m.group(3)
            additions = changes.count("+")
            deletions = changes.count("-")
            records.append({
                "path": path_str,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
            })
        else:
            records.append({
                "path": line.strip(),
                "additions": 0,
                "deletions": 0,
                "changes": 0,
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
    — the structured equivalent of ``git diff --stat``.

    **File mode** (*path* given): returns the hunks for that file as an
    array of hunk objects with ``old_start``, ``old_count``,
    ``new_start``, ``new_count``, and ``content`` (the hunk header +
    diff lines).  Supports *offset* / *limit* pagination.

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
        JSON string.  In summary mode: ``{files: [...], total_changes: N}``.
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
    """Return file-by-file diff summary."""
    cmd = f"cd {safe_wd} && git diff {safe_base}...HEAD --stat 2>/dev/null"
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
    files = _parse_diffstat(lines)

    # Parse total line: "N files changed, M insertions(+), K deletions(-)"
    total_additions = sum(f["additions"] for f in files)
    total_deletions = sum(f["deletions"] for f in files)

    return json.dumps({
        "files": files,
        "total_files": len(files),
        "total_additions": total_additions,
        "total_deletions": total_deletions,
    })


def _file_diff(container, safe_wd: str, safe_base: str, path: str, offset: int, limit: int) -> str:
    """Return per-file hunks with pagination."""
    safe_path = shlex.quote(path)
    # Get the full unified diff for the file
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

    # Parse hunks from unified diff
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
