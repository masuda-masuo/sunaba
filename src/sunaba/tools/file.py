"""File tools: write_file_sandbox, transform_file, copy_project, copy_file, read_file_range, list_files."""

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
    read_file,
    read_file_lines,
    transform_file_in_container,
    write_file,
)
from sunaba.journal import record_copy, record_tool_use
from sunaba.output_control import paginate_output, truncate_output
from sunaba.tools.common import _docker

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
) -> str | None:
    """Attempt whitespace-flexible matching.

    Strips leading/trailing whitespace from each line of *old_str* and
    slides over the file looking for a block whose stripped lines match.
    When found the file's original indentation is preserved and *new_str*
    is re-indented to fit.

    Returns the new file content on success, or ``None`` if no match
    was found.
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
    return result


def _build_near_miss_echo(existing: str, old_str: str, dest_path: str) -> str:
    """Build a near-miss error message with diff, context, and indentation hints.

    Uses a sliding-window line match to find the most similar region,
    shows at most 6 lines of unified diff, 3 lines of surrounding
    context, and explicitly flags indentation mismatches.
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
    # Cap diff output to at most 6 lines (+ header may show more)
    if len(diff_lines) > 6:
        diff_lines = diff_lines[:6] + ["... (diff truncated)"]
    diff_block = "\n".join(diff_lines) if diff_lines else "(identical content, whitespace differs)"

    # --- indentation hint ---
    indent_hint = ""
    if old_lines and matched_lines:
        old_first_indent = _get_line_indent(old_lines[0])
        file_first_indent = _get_line_indent(matched_lines[0])
        if old_first_indent != file_first_indent:
            indent_hint = (
                f"\n(Indentation mismatch: old_str indent={old_first_indent}, "
                f"file indent={file_first_indent})"
            )

    return (
        f"Error: old_str not found in {dest_path}.\n"
        f"Best matching region (similarity={best_ratio:.0%}):\n"
        f"{context_block}\n"
        f"Unified diff (old_str vs file region):\n"
        f"{diff_block}"
        f"{indent_hint}\n"
        "Tip: Use read_file_range first to confirm the exact content "
        "(including whitespace)."
    )


# ---------------------------------------------------------------------------
# write_file_sandbox
# ---------------------------------------------------------------------------


def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/home/sandbox",
    start_line: int | None = None,
    end_line: int | None = None,
    append: bool = False,
    old_str: str | None = None,
) -> str:
    """Write a file to the container. Supports full overwrite and partial updates.

    **Mode selection (pick exactly one):**

    ================= ===================================================
    Mode              Parameters
    ================= ===================================================
    Full overwrite    (none of the below) — writes *file_contents* as-is
    Line-range        ``start_line`` [+ ``end_line``] — replace lines
    Append            ``append=True`` — append to existing file
    String replace    ``old_str`` — replace exact text (see matching below)
    ================= ===================================================

    The three partial modes are mutually exclusive; when none is given
    the file is fully overwritten.  Line-range bounds are 1-indexed and
    inclusive; omitted *start_line* defaults to 1, omitted *end_line* to
    the last line.

    **old_str matching logic:**

    1. **Exact match** -- replaced only when *old_str* appears exactly
       once; multiple matches are rejected with the line numbers of each
       match (add surrounding context and retry).
    2. **Whitespace-flexible fallback** -- retried with per-line
       leading/trailing whitespace stripped; on success *file_contents*
       is re-indented to match the file's original indentation.
    3. **Near-miss echo** -- when nothing matches, the most similar
       region of the file is returned with line numbers.

    .. warning::

       **One operation = one logical unit.**  ``old_str`` matches the
       *exact string you provide* — if it contains multiple lines,
       **all** of them are replaced.  A common mistake is including
       an adjacent line you did not intend to touch (e.g. the line
       after a tool registration).  When removing a single item,
       keep ``old_str`` as short as uniquely identifying content.
       For removing multiple separate lines, use :func:`transform_file`
       or repeated single-line ``old_str`` calls.

    Args:
        container_id: 12-character container ID prefix.
        file_name: Name of the file to write.
        file_contents: Content to write.
        dest_dir: Destination directory in the container (default: ``/home/sandbox``).
        start_line: Start line for line-range replacement (1-indexed, inclusive).
        end_line: End line for line-range replacement (1-indexed, inclusive).
        append: When True, appends to the end of the file.
        old_str: When specified, replaces this string in the existing file.
            Performs uniqueness check, whitespace-flexible fallback, and near-miss echo (see above).

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
            else:
                # 2. Whitespace-flexible fallback
                result = _try_whitespace_flexible(
                    existing, old_str, file_contents,
                )
                if result is not None:
                    if result.startswith("Error:"):
                        return result
                    content = result
                else:
                    # 3. Near-miss echo
                    return _build_near_miss_echo(existing, old_str, dest_path)
        else:
            start = start_line - 1 if start_line is not None else 0
            end = end_line if end_line is not None else len(existing_lines)
            new_lines = file_contents.splitlines()
            content_lines = existing_lines[:start] + new_lines + existing_lines[end:]
            content = "\n".join(content_lines)
            if file_contents.endswith("\n"):
                content += "\n"

    try:
        write_file(container, container_id[:12], dest_path, content)
    except ValueError as e:
        return f"Error: {e}"
    return f"Written {len(content)} bytes to {dest_path}"


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/home/sandbox",
) -> str:
    """Copy a local directory (or file) into the container as a tar archive.

    Creates a tar archive of the local path in a temp directory and
    streams it into the container with ``put_archive``.

    The target directory inside the tar archive is named after the
    source directory itself (i.e. ``/home/sandbox/source_dir_name/...``).

    .. note::

       After copying, files are ``chown``-ed to the container's running
       user so they remain writable by the file-editing tools.

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default:
            ``/home/sandbox``).

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

    arcname = src_path.name or "project"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            tar.add(src_path, arcname=arcname)
        tmp.file.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(dest_dir, buf)
        except APIError as e:
            return f"Error: {e}"
        dest_path = f"{dest_dir}/{arcname}"
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
    dest_path: str = "/home/sandbox",
) -> str:
    """Copy a single local file into the container.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: ``/home/sandbox``).

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
    """Read lines from *file_path* starting at *offset*.

    Returns a JSON string with:
    - ``content`` (str): the requested lines
    - ``total_lines`` (int): total lines in the file
    - ``shown`` (int): lines returned
    - ``has_more`` (bool): whether more lines exist after this range
    - ``next_offset`` (int | None): offset for pagination

    .. hint::

       Use ``limit=-1`` to read all remaining lines from *offset*
       to end of file in one call.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.  Use ``-1`` to read
            all remaining lines from *offset*.
        start_line: 1-indexed start line (inclusive).  When set,
            *offset* and *limit* are derived from *start_line*
            and *end_line* instead.
        end_line: 1-indexed end line (inclusive).  When omitted,
            reads from *start_line* to end of file.
            Mutually exclusive with *offset* (use either
            start_line/end_line or offset/limit, not both).

    Returns:
        JSON string with file content and metadata, or an error
        message beginning with ``"Error:"``.

    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
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
    path: str = "/home/sandbox",
    max_depth: int = 3,
    pattern: str = "",
) -> str:
    """List files inside the container using ``find``.

    Returns a JSON array of file paths sorted alphabetically.
    Hidden files (dotfiles) and directories under ``.git`` are
    excluded.

    Args:
        container_id: 12-character container ID prefix.
        path: Directory path to list (default ``"/home/sandbox"``).
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
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
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
    """Edit a file imperatively by running Python that computes the new text.

    The **imperative** edit path: instead of providing the new bytes
    (:func:`write_file_sandbox`) or a diff (:func:`apply_patch`), you provide
    *code* that transforms the file's content.  Ideal for edits that the
    declarative tools handle poorly — bulk / repetitive / structural / computed
    changes (e.g. a regex applied to every occurrence, renaming a symbol,
    re-indenting, applying a value derived from the existing text).

    *code* is executed as a **complete Python module** inside the disposable
    sandbox container (never on the host).  The **only** requirement is that,
    once the module finishes executing, a top-level callable
    ``transform(text: str) -> str`` exists — you are free to define helper
    functions, classes, ``import`` modules, and any number of other top-level
    statements alongside it.  ``transform`` is called with the file's current
    text and must return the new text; the result is written back and a
    **unified diff of the change is returned** so you can verify the effect
    without a separate read-back.

    *code* is base64-encoded before transport, so quotes (including
    triple-quoted strings), backslashes, multibyte characters, and newlines
    need no escaping — pass the program as a single ``code`` string, exactly as
    you would write it in a ``.py`` file.

    Example — uppercase every TODO marker, using a helper::

        import re

        def _to_upper(m):
            return m.group(0).upper()

        def transform(text):
            return re.sub("todo", _to_upper, text, flags=re.IGNORECASE)

    .. warning::

       Always check the returned ``diff``; an over-broad pattern can
       change more than intended.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Absolute path to the file inside the container.
        code: Python source defining a top-level ``transform(text: str) -> str``
            (helper functions, classes, and ``import`` statements alongside it
            are fine; only the ``transform`` callable is required).
            Executed as a **full Python interpreter** (not a restricted DSL):
            ``__builtins__``, ``open()``, ``import``, ``subprocess``, etc.
            are all available inside the disposable sandbox container.
        max_lines: Maximum diff lines to show (summary truncation).
        offset: Line offset for paging through a large diff (0-indexed).
        limit: Maximum diff lines per page.

    Returns:
        JSON string.  On success: ``status="ok"``, ``changed`` (bool),
        ``diff`` (str, paginated) and diff metadata (``shown``,
        ``total_lines``, ``truncated``, ``next_offset``, ``has_more``).
        On failure: ``status="error"`` with ``error`` (and ``traceback`` when
        the caller's code raised).

    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            {"status": "error", "error": f"container {container_id[:12]} not found"}
        )
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
