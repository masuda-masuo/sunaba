"""File tools: write_file_sandbox, transform_file, edit_symbol, copy_project, copy_file, read_file_range, list_files."""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import posixpath
import shlex
import tarfile
import tempfile
from pathlib import Path

from docker.errors import APIError, NotFound

from sunaba.edit_verify import (
    _file_size_from_counts,
    edit_symbol_in_container,
    read_file,
    read_file_lines,
    transform_file_in_container,
    write_file,
)
from sunaba.journal import record_copy, record_tool_use
from sunaba.output_control import paginate_output, truncate_output
from sunaba.tools.common import WORKSPACE, _docker, container_not_found_error

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# write_file_sandbox  --  old_str helper functions
# ---------------------------------------------------------------------------


def _find_all_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    """Find all non-overlapping occurrences of *pattern* in *text*.

    Returns a list of ``(offset, line_number)`` tuples.
    """
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = text.find(pattern, idx)
        if idx == -1:
            break
        line_no = text[:idx].count("\n") + 1
        matches.append((idx, line_no))
        idx += 1
    return matches


def _get_line_indent(line: str) -> int:
    """Return the leading whitespace length of *line*."""
    return len(line) - len(line.lstrip())


def _reindent_lines(lines: list[str], delta: int) -> list[str]:
    """Apply an indentation *delta* (number of spaces) to each line.

    Empty/whitespace-only lines are passed through unchanged.
    A positive *delta* adds leading spaces; a negative *delta* removes them.
    """
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue
        if delta >= 0:
            result.append(" " * delta + line)
        else:
            remove = min(-delta, _get_line_indent(line))
            result.append(line[remove:])
    return result


def _try_whitespace_flexible(
    existing: str, old_str: str, new_str: str,
) -> tuple[str, int, int] | str | None:
    """Attempt whitespace-flexible matching.

    Strips leading/trailing whitespace from each line of *old_str* and
    slides over the file looking for a block whose stripped lines match.
    When found the file's original indentation is preserved and *new_str*
    is re-indented to fit.

    Returns ``(new_content, replaced_start_line, replaced_end_line)`` on
    success (1-indexed lines in the *new* content), an ``"Error: ..."``
    string when the match is ambiguous, or ``None`` if no match was found.
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    old_stripped = [line.strip() for line in old_lines]

    if len(old_lines) > len(existing_lines):
        return None

    matches: list[int] = []
    for i in range(len(existing_lines) - len(old_lines) + 1):
        chunk = existing_lines[i : i + len(old_lines)]
        if [line.strip() for line in chunk] == old_stripped:
            matches.append(i)

    if not matches:
        return None

    if len(matches) > 1:
        line_nos = ", ".join(str(m + 1) for m in matches[:10])
        suffix = "..." if len(matches) > 10 else ""
        return (
            f"Error: old_str matches at {len(matches)} locations "
            f"(lines {line_nos}{suffix}) after whitespace normalization. "
            "Add more surrounding context to make it unique."
        )

    i = matches[0]
    chunk = existing_lines[i : i + len(old_lines)]
    file_first_indent = _get_line_indent(chunk[0])
    old_first_indent = _get_line_indent(old_lines[0])
    delta = file_first_indent - old_first_indent
    reindented = _reindent_lines(new_str.splitlines(), delta)
    new_content = "\n".join(reindented)

    # Build character offsets to do a string-level replacement
    # (preserves trailing whitespace and file structure).
    pos = 0
    line_starts: list[int] = []
    for line in existing_lines:
        line_starts.append(pos)
        pos += len(line) + 1  # +1 for newline
    # offset right after the last matched line
    start_offset = line_starts[i]
    end_idx = i + len(old_lines)
    if end_idx < len(line_starts):
        end_offset = line_starts[end_idx]
    else:
        end_offset = len(existing)

    result = existing[:start_offset] + new_content + existing[end_offset:]
    if existing.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    replaced_start = i + 1
    replaced_end = i + max(len(reindented), 1)
    return result, replaced_start, replaced_end


# Unified diff display limits for the near-miss echo: old_str blocks of up
# to _NEAR_MISS_FULL_DIFF_MAX_LINES lines get an untruncated diff; longer
# ones are capped at _NEAR_MISS_DIFF_CAP diff lines.
_NEAR_MISS_FULL_DIFF_MAX_LINES = 50
_NEAR_MISS_DIFF_CAP = 30


def _build_first_mismatch_report(
    old_lines: list[str], matched_lines: list[str], best_start: int,
) -> str:
    """Report the first line where *old_lines* and *matched_lines* diverge.

    Compares the whitespace-stripped lines with
    :meth:`difflib.SequenceMatcher.get_opcodes` so that inserted or
    missing lines (e.g. a duplicated line in old_str) still point at the
    first real divergence instead of shifting every subsequent line.
    Lines are shown with ``repr()`` to make tabs, spaces, and other
    invisible characters visible.  *best_start* is the 0-indexed file
    line of ``matched_lines[0]`` used to report real file line numbers.

    Returns an empty string when the stripped lines are identical
    (whitespace-only mismatch, normally handled by the flexible matcher).
    """
    old_stripped = [line.strip() for line in old_lines]
    matched_stripped = [line.strip() for line in matched_lines]
    sm = difflib.SequenceMatcher(None, old_stripped, matched_stripped)
    for tag, i1, _i2, j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            return (
                f"First mismatch: old_str line {i1 + 1} "
                f"vs file line {best_start + j1 + 1}\n"
                f"  old_str: {old_lines[i1]!r}\n"
                f"  file:    {matched_lines[j1]!r}"
            )
        if tag == "delete":
            return (
                f"First mismatch: old_str line {i1 + 1}: "
                f"{old_lines[i1]!r} has no counterpart in the file region"
            )
        # tag == "insert"
        return (
            f"First mismatch: file line {best_start + j1 + 1}: "
            f"{matched_lines[j1]!r} has no counterpart in old_str"
        )
    return ""


def _build_near_miss_echo(existing: str, old_str: str, dest_path: str) -> str:
    """Build a near-miss error message with diff, context, and first mismatch.

    Uses a sliding-window line match to find the most similar region,
    shows a unified diff (full for old_str blocks of up to 50 lines,
    capped at 30 diff lines beyond that), 3 lines of surrounding
    context, and pinpoints the first mismatching line (issue #580).
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    n_old = len(old_lines)
    n_existing = len(existing_lines)

    # --- find best-matching block via sliding window ---
    best_ratio = 0.0
    best_start = 0  # line index in existing_lines
    best_end = 0

    if n_old <= n_existing:
        for i in range(n_existing - n_old + 1):
            block = "\n".join(existing_lines[i:i + n_old])
            sm = difflib.SequenceMatcher(None, old_str, block)
            # quick_ratio() is an upper bound on ratio(); skip windows
            # that cannot beat the current best (issue #580).
            if sm.quick_ratio() <= best_ratio:
                continue
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_start = i
                best_end = i + n_old
    elif n_existing > 0:
        # old_str longer than file -- compare with whole file
        sm = difflib.SequenceMatcher(None, old_str, existing)
        best_ratio = sm.ratio()
        best_start = 0
        best_end = n_existing

    # --- build context (3 lines before / after) ---
    ctx_start = max(0, best_start - 3)
    ctx_end = min(n_existing, best_end + 3)
    context_lines: list[str] = []
    for i in range(ctx_start, ctx_end):
        prefix = ">>>" if best_start <= i < best_end else "   "
        context_lines.append(f"{prefix} {i + 1:4d} | {existing_lines[i]}")
    context_block = "\n".join(context_lines)

    # --- unified diff (limited to 6 lines) ---
    matched_lines = existing_lines[best_start:best_end] if best_end > best_start else []
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            matched_lines,
            fromfile="old_str (provided)",
            tofile=f"{dest_path} (file)",
            lineterm="",
        )
    )
    # Show the full diff for old_str blocks of up to 50 lines; beyond
    # that cap the diff at 30 lines so the interesting part survives
    # (issue #580 -- the old 6-line cap hid the actual difference).
    if (
        len(old_lines) > _NEAR_MISS_FULL_DIFF_MAX_LINES
        and len(diff_lines) > _NEAR_MISS_DIFF_CAP
    ):
        remaining = len(diff_lines) - _NEAR_MISS_DIFF_CAP
        diff_lines = diff_lines[:_NEAR_MISS_DIFF_CAP] + [
            f"... (truncated, {remaining} more lines)"
        ]
    diff_block = "\n".join(diff_lines) if diff_lines else "(identical content, whitespace differs)"

    # --- first mismatching line (replaces the old indentation hint) ---
    mismatch_report = _build_first_mismatch_report(
        old_lines, matched_lines, best_start,
    )
    mismatch_section = f"\n{mismatch_report}" if mismatch_report else ""

    return (
        f"Error: old_str not found in {dest_path}.\n"
        f"Best matching region (similarity={best_ratio:.0%}):\n"
        f"{context_block}\n"
        f"Unified diff (old_str vs file region):\n"
        f"{diff_block}"
        f"{mismatch_section}\n"
        "Tip: Use read_file_range first to confirm the exact content "
        "(including whitespace)."
    )


# Success echo limits: +-2 context lines around the replaced region and a
# 30-row overall cap with the middle elided.
_SUCCESS_ECHO_CONTEXT = 2
_SUCCESS_ECHO_MAX_ROWS = 30


def _build_success_echo(
    content: str, dest_path: str, rep_start: int, rep_end: int,
) -> str:
    """Echo the post-edit region after a successful old_str replacement.

    Shows the replaced lines (marked ``>>>``) with line numbers and
    +-2 lines of context so the model keeps a ground-truth image of the
    file right after the edit instead of drifting across batch edits
    (issue #580).  The echo is capped at 30 rows; the middle is elided.
    """
    lines = content.splitlines()
    if not lines:
        return f"Written {len(content)} bytes to {dest_path}"
    rep_start = max(1, min(rep_start, len(lines)))
    rep_end = max(rep_start, min(rep_end, len(lines)))
    if rep_start == rep_end:
        span = f"replaced line {rep_start}"
    else:
        span = f"replaced lines {rep_start}-{rep_end}"

    ctx_start = max(1, rep_start - _SUCCESS_ECHO_CONTEXT)
    ctx_end = min(len(lines), rep_end + _SUCCESS_ECHO_CONTEXT)
    rows: list[str] = []
    for ln in range(ctx_start, ctx_end + 1):
        prefix = ">>>" if rep_start <= ln <= rep_end else "   "
        rows.append(f"{prefix} {ln:4d} | {lines[ln - 1]}")

    if len(rows) > _SUCCESS_ECHO_MAX_ROWS:
        keep = (_SUCCESS_ECHO_MAX_ROWS - 1) // 2
        omitted = len(rows) - 2 * keep
        rows = rows[:keep] + [f"... ({omitted} lines)"] + rows[-keep:]

    return (
        f"Written {len(content)} bytes to {dest_path} ({span})\n"
        + "\n".join(rows)
    )


# ---------------------------------------------------------------------------
# write_file_sandbox
# ---------------------------------------------------------------------------


def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = WORKSPACE,
    start_line: int | None = None,
    end_line: int | None = None,
    append: bool = False,
    old_str: str | None = None,
) -> str:
    """Write or partially edit a file in the container.

    Edit modes are mutually exclusive; with none given the file is
    fully overwritten.  Line-range: start_line[/end_line], 1-indexed
    inclusive.  Append: append=True.  String replace: old_str.
    Line-range and append keep the file's trailing newline as it was.

    old_str contract: multiple matches are rejected with their line
    numbers (add context, retry); an inexact match retries with
    per-line whitespace stripped and re-indents on success; no match
    returns the nearest-miss region with line numbers plus the first
    mismatching line; a successful replace echoes the post-edit region
    with line numbers (ground truth for the next edit).  old_str matches
    the exact string you provide -- if it spans several lines, ALL of
    them are replaced, so keep it minimal and unique.  For several
    separate edits use repeated calls or transform_file.

    Args:
        container_id: Container ID prefix.
        file_name: Name of the file to write.
        file_contents: Content to write.
        dest_dir: Destination directory.
        start_line: Line-range start (1-indexed, inclusive).
        end_line: Line-range end (1-indexed, inclusive; default last line).
        append: Append to the end of the existing file.
        old_str: Exact text to replace with file_contents (matching
            contract above).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = posixpath.join(dest_dir, file_name)

    # Validate mutual exclusivity
    has_line_range = start_line is not None or end_line is not None
    mode_count = sum([append, old_str is not None, has_line_range])
    if mode_count > 1:
        return "Error: start_line/end_line, append, and old_str are mutually exclusive"

    if old_str is not None and old_str == "":
        return "Error: old_str must not be empty"
    if start_line is not None and start_line < 1:
        return "Error: start_line must be >= 1"

    content = file_contents
    # 1-indexed (start, end) lines of the replaced region in the new
    # content; set only for successful old_str edits (issue #580).
    replaced_span: tuple[int, int] | None = None

    # For partial updates, read existing content
    if append or old_str is not None or has_line_range:
        try:
            existing = read_file(container, dest_path)
        except ValueError:
            return f"Error: file {dest_path} not found"
        existing_lines = existing.splitlines()

        # Validate bounds
        if start_line is not None and start_line > len(existing_lines):
            return f"Error: start_line {start_line} exceeds file length ({len(existing_lines)} lines)"
        if end_line is not None:
            if end_line > len(existing_lines):
                return f"Error: end_line {end_line} exceeds file length ({len(existing_lines)} lines)"
            if start_line is not None and start_line > end_line:
                return "Error: start_line is greater than end_line"

        if append:
            sep = "\n" if existing else ""
            content = existing.rstrip("\n") + sep + file_contents
            # rstrip() would also swallow the file's final newline (#570).
            if existing.endswith("\n") and not content.endswith("\n"):
                content += "\n"
        elif old_str is not None:
            # 1. Exact match with uniqueness check
            exact_matches = _find_all_matches(existing, old_str)
            if len(exact_matches) > 1:
                line_nos = ", ".join(str(m[1]) for m in exact_matches[:10])
                suffix = "..." if len(exact_matches) > 10 else ""
                return (
                    f"Error: old_str matches at {len(exact_matches)} locations "
                    f"(lines {line_nos}{suffix}). "
                    "Add more surrounding context to make it unique."
                )
            if len(exact_matches) == 1:
                idx = exact_matches[0][0]
                content = (
                    existing[:idx]
                    + file_contents
                    + existing[idx + len(old_str) :]
                )
                rep_start = existing[:idx].count("\n") + 1
                end_offset = idx + len(file_contents)
                rep_end = content[:end_offset].count("\n") + 1
                if file_contents.endswith("\n") and file_contents:
                    rep_end -= 1
                replaced_span = (rep_start, max(rep_start, rep_end))
            else:
                # 2. Whitespace-flexible fallback
                result = _try_whitespace_flexible(
                    existing, old_str, file_contents,
                )
                if isinstance(result, str):
                    return result  # ambiguous-match error
                if result is not None:
                    content, rep_start, rep_end = result
                    replaced_span = (rep_start, rep_end)
                else:
                    # 3. Near-miss echo
                    return _build_near_miss_echo(existing, old_str, dest_path)
        else:
            start = start_line - 1 if start_line is not None else 0
            end = end_line if end_line is not None else len(existing_lines)
            new_lines = file_contents.splitlines()
            content_lines = existing_lines[:start] + new_lines + existing_lines[end:]
            content = "\n".join(content_lines)
            # The trailing newline belongs to the file, not to the replacement
            # snippet: splitlines() drops it, so restore it from *existing*
            # (#570).  A snippet that ends in "\n" still forces one, so a file
            # that lacked the final newline can gain it deliberately.
            if existing.endswith("\n") or file_contents.endswith("\n"):
                content += "\n"

    try:
        write_file(container, container_id[:12], dest_path, content)
    except ValueError as e:
        return f"Error: {e}"
    if replaced_span is not None:
        return _build_success_echo(content, dest_path, *replaced_span)
    return f"Written {len(content)} bytes to {dest_path}"


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = WORKSPACE,
) -> str:
    """Copy a local directory into the container as a tar archive.

    The directory's *contents* land in *dest_dir*, so the copied project
    becomes the git root the container already works in -- verify and publish
    find it without being told where it is.

    .. note::

       After copying, files are ``chown``-ed to the container's running
       user so they remain writable by the file-editing tools.

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default: the
            workspace, ``/workspace``).

    Returns:
        Success or error message.

    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src_path = Path(local_src_dir).resolve()
    if not src_path.exists():
        return f"Error: {local_src_dir} does not exist"
    if not src_path.is_dir():
        return f"Error: {local_src_dir} is not a directory"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            tar.add(src_path, arcname=".")
        tmp.file.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(dest_dir, buf)
        except APIError as e:
            return f"Error: {e}"
        dest_path = dest_dir
        try:
            container.exec_run(
                ["sh", "-c", f"chown -R $(id -u):$(id -g) {shlex.quote(dest_path)}"]
            )
        except Exception as e:
            logger.debug("chown failed for %s: %s", dest_path, e)
        record_copy(
            container_id[:12], "copy_project", local_src_dir, dest_path
        )
        return (
            f"Copied {local_src_dir} to {dest_path} "
            f"in container {container_id[:12]}"
        )
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = WORKSPACE,
) -> str:
    """Copy a single local file into the container.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: the workspace, ``/workspace``).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src = Path(local_src_file).resolve()
    if not src.exists():
        return f"Error: {local_src_file} does not exist"
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    dest = dest_path
    if not dest.endswith("/") and not dest.endswith(src.name):
        dest = posixpath.join(dest_path, src.name)

    parent_dir = posixpath.dirname(dest)
    base_name = posixpath.basename(dest)

    with open(src, "rb") as f:
        data = f.read()

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=base_name)
        info.size = len(data)
        info.mtime = int(src.stat().st_mtime)
        tar.addfile(info, io.BytesIO(data))

    try:
        container.put_archive(parent_dir, tar_stream.getvalue())
    except APIError as e:
        return f"Error: {e}"
    try:
        container.exec_run(
            ["sh", "-c", f"chown -R $(id -u):$(id -g) {shlex.quote(dest)}"]
        )
    except Exception as e:
        logger.debug("chown failed for %s: %s", dest, e)
    record_copy(container_id[:12], "copy_file", local_src_file, dest)
    return f"Copied {local_src_file} to {dest} in container {container_id[:12]}"


# ---------------------------------------------------------------------------
# read_file_range
# ---------------------------------------------------------------------------


def read_file_range(
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read lines from *file_path* inside the container.

    Args:
        container_id: Container ID prefix.
        file_path: File path inside the container.
        offset: 0-indexed start line.
        limit: Max lines to return; -1 reads to end of file.
        start_line: 1-indexed inclusive start. start_line/end_line and
            offset/limit are mutually exclusive pairs.
        end_line: 1-indexed inclusive end; default end of file.

    Returns:
        JSON: content, total_lines, shown, has_more, next_offset.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    if start_line is not None and offset != 0:
        return json.dumps({
            "status": "error",
            "error": "start_line and offset are mutually exclusive. "
            "Use start_line/end_line (1-indexed) or offset/limit (0-indexed), not both."
        })
    if start_line is not None and start_line < 1:
        return json.dumps({"status": "error", "error": "start_line must be >= 1 (1-indexed)"})
    if end_line is not None and start_line is not None and end_line < start_line:
        return json.dumps({"status": "error", "error": "end_line must be >= start_line"})
    resolved_offset = offset
    resolved_limit = limit
    if start_line is not None:
        resolved_offset = start_line - 1
        if end_line is not None:
            resolved_limit = end_line - start_line + 1
        else:
            resolved_limit = -1
    record_tool_use(
        container_id[:12],
        "read_file_range",
        {"file_path": file_path},
    )
    result = read_file_lines(
        _, file_path, offset=resolved_offset, limit=resolved_limit
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def list_files(
    container_id: str,
    path: str = WORKSPACE,
    max_depth: int = 3,
    pattern: str = "",
) -> str:
    """List files inside the container using ``find``.

    Returns a JSON array of file paths sorted alphabetically.
    Hidden files (dotfiles) and directories under ``.git`` are
    excluded.

    Args:
        container_id: 12-character container ID prefix.
        path: Directory path to list (default: the workspace,
            ``"/workspace"``).
        max_depth: Maximum directory depth (default 3).
        pattern: Optional glob pattern to filter files
            (e.g. ``"*.py"``, ``"*.md"``).

    Returns:
        JSON string with ``path``, ``total``, and ``files`` list.
        On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    safe_path = shlex.quote(path)

    name_filter = ""
    if pattern:
        name_filter = f" -name {shlex.quote(pattern)}"

    cmd = (
        f"find {safe_path} -maxdepth {max_depth}"
        f" -not -path '*/\\.*'"
        f" -type f{name_filter}"
        f" | sort"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({"status": "error", "error": stderr_text or stdout_text})

    record_tool_use(
        container_id[:12],
        "list_files",
        {"path": path, "max_depth": max_depth, "pattern": pattern},
    )
    files = [f for f in stdout_text.strip().split("\n") if f]
    return json.dumps({
        "path": path,
        "total": len(files),
        "files": files,
    })


# ---------------------------------------------------------------------------
# transform_file (moved from verify.py, issue #258)
# ---------------------------------------------------------------------------


def transform_file(
    container_id: str,
    file_path: str,
    code: str,
    max_lines: int = 200,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Edit a file by running Python that computes the new text.

    code executes as a complete module inside the container (never on
    the host); when it finishes, a top-level callable
    transform(text: str) -> str must exist (helpers and imports are
    fine).  The file's text goes in, the returned text is written back,
    and a unified diff is returned -- always check it: an over-broad
    pattern can change more than intended.  Prefer write_file_sandbox
    old_str for a single known replacement; use this for bulk, pattern,
    or computed edits.  Example::

        import re
        def transform(text):
            return re.sub("todo", "TODO", text)

    Args:
        container_id: Container ID prefix.
        file_path: Absolute path inside the container.
        code: Python source defining transform(text) -> str. Transported
            base64-encoded, so quotes, backslashes, and newlines need no
            escaping -- write it exactly as a .py file.
        max_lines: Max diff lines shown.
        offset: Diff paging offset (0-indexed).
        limit: Max diff lines per page.

    Returns:
        JSON: status, changed, diff (paginated, with paging metadata);
        on failure error, and traceback when your code raised.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(
        container_id[:12],
        "transform_file",
        {"file_path": file_path},
    )
    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") == "ok" and result.get("changed"):
        display, meta = truncate_output(
            result.get("diff", ""),
            max_lines=max_lines,
            verbose="full",
        )
        page = paginate_output(display, offset=offset, limit=limit)
        return json.dumps({
            "status": "ok",
            "changed": True,
            "diff": page.content,
            "shown": meta.shown,
            "total_lines": meta.total_lines,
            "truncated": meta.truncated,
            "next_offset": page.next_offset,
            "has_more": page.has_more,
            "file_size": _file_size_from_counts(
                int(result.get("new_size", 0)), int(result.get("new_lines", 0))
            ),
        })

    # Unchanged (or error-free no-op) results still surface file_size so the
    # model sees the current size without a separate read (issue #187, ①).
    if result.get("status") == "ok" and not result.get("changed"):
        result["file_size"] = _file_size_from_counts(
            int(result.get("new_size", 0)), int(result.get("new_lines", 0))
        )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# edit_symbol (issue #581)
# ---------------------------------------------------------------------------


def edit_symbol(
    container_id: str,
    file_path: str,
    symbol: str,
    new_code: str,
    line: int | None = None,
    preserve: str = "decorators+docstring",
) -> str:
    """Replace or delete a Python definition by name -- no old_str needed.

    Locates *symbol* ("foo", "Foo.bar") via AST and replaces the whole
    definition (decorators included) with *new_code*, re-indented to fit.
    new_code="" deletes the definition.  SyntaxError in the result is
    rejected before writing.  Use line=<lineno> to disambiguate overloads.
    Python files only.

    *preserve* controls what of the old definition to keep: decorators
    and/or docstring.  If *new_code* already carries them, old ones are
    not duplicated.  Comments inside the body are never preserved: the
    AST (``ast.parse``) discards them.  Keep comments in *new_code* if
    they matter.

    Args:
        container_id: Container ID prefix.
        file_path: Absolute path inside the container (.py only).
        symbol: Definition name, optionally qualified ("foo", "Foo.bar",
            "outer.inner").
        new_code: Replacement definition source; "" deletes the symbol.
            Whitespace-only is rejected.
        line: Disambiguates same-name definitions: any line number inside
            the intended definition (decorators included).
        preserve: Old definition parts to keep:
            "decorators+docstring" (default);
            "decorators" / "docstring" / "none".

    Returns:
        JSON: status, resolved (qualname/kind/start_line/end_line),
        changed, diff (truncated past 120 lines), truncated, file_size;
        on failure error.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    if not file_path.endswith(".py"):
        return json.dumps({
            "status": "error",
            "error": "Error: edit_symbol supports .py files only; "
            "use write_file_sandbox or transform_file",
        })

    record_tool_use(
        container_id[:12],
        "edit_symbol",
        {"file_path": file_path, "symbol": symbol},
    )
    result = edit_symbol_in_container(
        client, container_id, file_path, symbol, new_code, line, preserve
    )

    if result.get("status") != "ok":
        return json.dumps(result)

    file_size = _file_size_from_counts(
        int(result.get("new_size", 0)), int(result.get("new_lines", 0))
    )
    if not result.get("changed"):
        return json.dumps({
            "status": "ok",
            "resolved": result.get("resolved"),
            "changed": False,
            "diff": "",
            "truncated": False,
            "file_size": file_size,
        })

    display, meta = truncate_output(result.get("diff", ""), max_lines=120)
    return json.dumps({
        "status": "ok",
        "resolved": result.get("resolved"),
        "changed": True,
        "diff": display,
        "truncated": meta.truncated,
        "file_size": file_size,
    })
