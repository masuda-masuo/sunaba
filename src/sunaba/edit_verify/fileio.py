"""Container file I/O operations for sandbox containers.

Provides read, write, and line-range access to files inside
disposable sandbox containers.
"""

from __future__ import annotations

import io
import posixpath
import shlex
import tarfile
import time
from typing import Any

from sunaba.journal import record_file_write

from .paths import _is_test_file
from .shell import _quote_path

# ---------------------------------------------------------------------------
# Container file operations
# ---------------------------------------------------------------------------


def read_file(container: Any, file_path: str) -> str:
    """Read the full content of *file_path* from the sandbox container.

    Returns:
        File content as a string.

    Raises:
        ValueError: Container not found or file read error.
    """
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"cat {_quote_path(file_path)}"],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise ValueError(
            f"Failed to read {file_path}: exit code {exit_code}\n{stderr_text}"
        )
    return stdout_text


def _compute_file_size(text: str) -> dict[str, int]:
    """Compute file-size metadata for LLM awareness (issue #187, ① only).

    Returns ``{lines, bytes, approx_tokens}``.  ``lines`` is the true line
    count (newline-count convention); note this is a *different* measure from
    a pagination ``total_lines`` that counts the trailing segment after a
    final newline, so the two can differ by one for newline-terminated files.
    ``approx_tokens`` is a rough ``bytes // 4`` estimate for token-cost
    awareness by the model.
    """
    encoded = text.encode("utf-8")
    n_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return _file_size_from_counts(len(encoded), n_lines)


def _file_size_from_counts(n_bytes: int, n_lines: int) -> dict[str, int]:
    """Build file-size metadata from precomputed byte/line counts.

    Used where the full text isn't available (e.g. ``transform_file`` only
    has the runner's ``new_size`` / ``new_lines``), keeping the
    ``approx_tokens`` formula in one place shared with
    :func:`_compute_file_size`.
    """
    return {"lines": n_lines, "bytes": n_bytes, "approx_tokens": n_bytes // 4}




def write_file(container: Any, container_id_short: str, file_path: str, content: str) -> None:
    """Write *content* to *file_path* in the sandbox container.

    Ensures the parent directory exists and records the write
    in the execution journal (Issue #96).
    """
    if not file_path.startswith("/"):
        raise ValueError(f"file_path must be absolute: {file_path!r}")
    canon = posixpath.normpath(file_path)
    if ".." in canon.split(posixpath.sep):
        raise ValueError(f"Path traversal detected: {file_path!r}")

    # Stream the content via a tar archive (put_archive) instead of embedding
    # it in the shell argv.  Passing the (base64-encoded) bytes as a single
    # argv string trips Linux's MAX_ARG_STRLEN limit (128 KiB per argument),
    # which made writes of large files fail with "argument list too long"
    # (Issue #144).  put_archive streams over the Docker HTTP API body and has
    # no such limit.
    parent_dir = posixpath.dirname(file_path) or "/"

    # Ensure the parent directory exists (no file content in argv here).
    mk_code, mk_out = container.exec_run(
        ["/bin/sh", "-c", f"mkdir -p {_quote_path(parent_dir)}"],
        stdout=True,
        stderr=True,
    )
    if mk_code != 0:
        _, mk_err = mk_out if isinstance(mk_out, tuple) else (None, mk_out)
        mk_text = mk_err.decode("utf-8", errors="replace") if mk_err else ""
        raise ValueError(
            f"Failed to create parent dir for {file_path}: "
            f"exit code {mk_code}\n{mk_text}"
        )

    # Preserve ownership/mode: keep an existing file's, otherwise inherit the
    # parent directory's owner so the new file is not left owned by root.
    uid, gid, mode = _owner_for_write(container, file_path)

    data = content.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=posixpath.basename(file_path))
        info.size = len(data)
        info.mode = mode
        info.uid = uid
        info.gid = gid
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))

    try:
        ok = container.put_archive(parent_dir, tar_stream.getvalue())
    except Exception as e:
        raise ValueError(f"Failed to write {file_path}: {e}")
    if not ok:
        raise ValueError(f"Failed to write {file_path}: put_archive returned False")

    record_file_write(
        container_id_short,
        posixpath.basename(file_path),
        posixpath.dirname(file_path) or "/",
        len(content),
        is_test=_is_test_file(file_path),
    )






def _owner_for_write(
    container: Any, file_path: str
) -> tuple[int, int, int]:
    """Resolve ``(uid, gid, mode)`` for a file about to be written via put_archive.

    ``put_archive`` extracts tar entries with the ownership recorded in the
    archive (root:root by default), so we set it explicitly: an existing file
    keeps its own uid/gid/mode; a new file uses the container's running user
    so it remains writable by other tools (Issue #372).  Falls back to
    ``999, 999, 0o644`` when ``stat`` is unavailable.
    """
    def _stat(path: str, fmt: str) -> list[str] | None:
        code, out = container.exec_run(
            ["/bin/sh", "-c", f"stat -c {shlex.quote(fmt)} {_quote_path(path)}"],
            stdout=True,
            stderr=True,
        )
        stdout_part = out[0] if isinstance(out, tuple) else out
        if code != 0 or not stdout_part:
            return None
        return stdout_part.decode("utf-8", errors="replace").split()

    existing = _stat(file_path, "%u %g %a")
    if existing and len(existing) == 3:
        try:
            return int(existing[0]), int(existing[1]), int(existing[2], 8)
        except ValueError:
            pass

    # New file: own it as the running user so uid-999 tools (sandbox_exec,
    # transform_file, git via publish) can write it afterwards.  Read the uid
    # with ``id``, not ``stat /proc/self``: ``/proc/self`` is a root-owned
    # symlink and ``stat`` does not dereference by default, so it reported
    # 0:0 (root) and left new files unwritable by the sandbox user (Issue #642).
    code, out = container.exec_run(
        ["/bin/sh", "-c", "id -u; id -g"], stdout=True, stderr=True
    )
    stdout_part = out[0] if isinstance(out, tuple) else out
    if code == 0 and stdout_part:
        # ``id -u; id -g`` prints two newline-separated tokens ("999\n999\n");
        # split() on whitespace yields exactly [uid, gid] on success.
        ids = stdout_part.decode("utf-8", errors="replace").split()
        if len(ids) == 2:
            try:
                return int(ids[0]), int(ids[1]), 0o644
            except ValueError:
                pass

    return 999, 999, 0o644


def read_file_lines(
    container: Any,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Read lines from *file_path* starting at *offset*.

    When *limit* is a positive integer, reads up to that many lines.
    When *limit* is ``-1``, reads all lines from *offset* to the end.

    Returns a dict with:
    - ``content`` (str): the requested lines joined by newline
    - ``total_lines`` (int): total number of lines in the file
    - ``shown`` (int): number of lines returned
    - ``has_more`` (bool): whether there are more lines after this range
    - ``next_offset`` (int | None): offset for the next page (if any)
    - ``error`` (str | None): error message if the read failed

    Args:
        container: Docker container object.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.  Use ``-1`` to read
            all remaining lines from *offset*.

    Returns:
        A dict with content and pagination metadata.
    """
    try:
        content = read_file(container, file_path)
    except ValueError as e:
        return {"error": str(e)}

    lines = content.split("\n")
    total = len(lines)

    if limit == -1:
        page_lines = lines[offset:]
        shown = max(0, total - offset)
    else:
        page_lines = lines[offset : offset + limit]
        shown = len(page_lines)
    next_offset = offset + limit
    has_more = limit != -1 and next_offset < total

    return {
        "content": "\n".join(page_lines),
        "total_lines": total,
        "shown": shown,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "file_size": _compute_file_size(content),
        "error": None,
    }
