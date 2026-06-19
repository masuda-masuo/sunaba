"""Edit/Verify subsystem: minimal edit loop primitives for sandbox containers.

Provides low-level file editing and verification tools that operate on
disposable sandbox containers (not the real repository).  These tools
form the core of the minimal edit loop:

    search_in_container → read_file_range → apply_patch
    → lint/type_check → rerun_failed

By sending only diffs and reading only the needed lines, each iteration
consumes only hundreds of tokens instead of thousands.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from code_sandbox_mcp.journal import record_file_write


# ---------------------------------------------------------------------------
# Unified diff parsing and application
# ---------------------------------------------------------------------------

#: Regex for unified diff hunk headers: ``@@ -old_start,old_count +new_start,new_count @@``
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Regex for hunk body lines: `` ` ` (context), ``-`` (remove), ``+`` (add)
_HUNK_LINE_RE = re.compile(r"^([ +\-])")


def _parse_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Parse a unified diff string into a list of hunk dicts.

    Each hunk dict has:
    - ``old_start`` (int): 1-indexed start line in original file
    - ``old_count`` (int): number of original lines in hunk
    - ``new_start`` (int): 1-indexed start line in new file
    - ``new_count`` (int): number of new lines in hunk
    - ``lines`` (list[str]): hunk body lines with ``+``, ``-``, `` `` prefixes
    """
    hunks: list[dict[str, Any]] = []
    for line in diff_text.split("\n"):
        m = _HUNK_HEADER_RE.match(line)
        if m:
            hunks.append(
                {
                    "old_start": int(m.group(1)),
                    "old_count": int(m.group(2) or 1),
                    "new_start": int(m.group(3)),
                    "new_count": int(m.group(4) or 1),
                    "lines": [],
                }
            )
            continue
        if hunks and _HUNK_LINE_RE.match(line):
            hunks[-1]["lines"].append(line)
    return hunks


def apply_unified_diff(content: str, diff_text: str) -> str:
    """Apply a unified diff to *content* and return the result.

    Args:
        content: Original file content (string with newlines).
        diff_text: Unified diff string.

    Returns:
        The patched content.

    Raises:
        ValueError: If the diff is malformed or cannot be applied
            (e.g. context lines do not match).
    """
    if not diff_text.strip():
        return content  # Empty diff → no change

    hunks = _parse_hunks(diff_text)
    if not hunks:
        return content  # No hunks → no change

    original_ends_with_newline = content.endswith("\n")
    lines = content.split("\n")

    # When the original content ends with \n, split() produces a trailing
    # empty element (e.g. "a\nb\n" → ["a", "b", ""]).  Removing it here
    # avoids double trailing newlines after join.
    if original_ends_with_newline and lines and lines[-1] == "":
        lines = lines[:-1]

    # Apply hunks in reverse order (bottom to top) so line offsets in
    # earlier hunks remain valid.
    for hunk in reversed(hunks):
        old_start = hunk["old_start"] - 1  # Convert to 0-indexed
        old_count = hunk["old_count"]
        hunk_lines = hunk["lines"]

        # --- Validate context ---
        idx = old_start
        for hline in hunk_lines:
            if idx >= len(lines) and not hline.startswith("+"):
                raise ValueError(
                    f"Hunk references line {idx + 1} but file has only "
                    f"{len(lines)} line(s)"
                )
            if hline.startswith(" ") or hline.startswith("-"):
                expected = hline[1:]  # Strip prefix
                actual = lines[idx]
                if actual != expected:
                    raise ValueError(
                        f"Context mismatch at line {idx + 1}:\n"
                        f"  expected: {expected!r}\n"
                        f"  actual:   {actual!r}"
                    )
                idx += 1
            elif hline.startswith("+"):
                pass  # Addition, nothing to check
            elif hline.startswith("\\"):
                pass  # No-newline marker, skip

        # --- Apply the hunk ---
        before = lines[:old_start]
        after = (
            lines[old_start + old_count :]
            if old_start + old_count <= len(lines)
            else []
        )

        new_lines: list[str] = []
        for hline in hunk_lines:
            if hline.startswith(" ") or hline.startswith("-"):
                if not hline.startswith("-"):
                    new_lines.append(hline[1:])  # Context: keep
                # Removal: skip
            elif hline.startswith("+"):
                new_lines.append(hline[1:])  # Addition: insert
            # \\ no-newline markers are ignored

        lines = before + new_lines + after

    # Preserve trailing newline behaviour
    result = "\n".join(lines)
    if original_ends_with_newline:
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Container file operations
# ---------------------------------------------------------------------------


def _read_file(client: Any, container_id: str, file_path: str) -> str:
    """Read the full content of *file_path* from the sandbox container.

    Returns:
        File content as a string.

    Raises:
        ValueError: Container not found or file read error.
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        raise ValueError(f"Container {container_id[:12]} not found: {e}") from e

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


def write_file(client: Any, container_id: str, file_path: str, content: str) -> None:
    """Write *content* to *file_path* in the sandbox container.

    Ensures the parent directory exists and records the write
    in the execution journal (Issue #96).
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        raise ValueError(f"Container {container_id[:12]} not found: {e}") from e

    import base64

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent = _quote_path(os.path.dirname(file_path) or ".")
    cmd = f"mkdir -p {parent} && echo {encoded} | base64 -d > {_quote_path(file_path)}"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    _, stderr_part = output if isinstance(output, tuple) else (None, output)
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise ValueError(
            f"Failed to write {file_path}: exit code {exit_code}\n{stderr_text}"
        )
    record_file_write(
        container_id[:12],
        os.path.basename(file_path),
        os.path.dirname(file_path) or "/",
        len(content),
    )


def _quote_path(path: str) -> str:
    """Shell-escape a file path for use in a command string."""
    return shlex.quote(path)


# ---------------------------------------------------------------------------
# Search in container (lexical / structural)
# ---------------------------------------------------------------------------

#: Regex for standard grep output: ``file:line:text``
_GREP_OUTPUT_RE = re.compile(r"^([^:]+):(\d+):(.*)$")


def search_files(
    client: Any,
    container_id: str,
    pattern: str,
    path: str = "/",
    mode: str = "lexical",
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Search for *pattern* inside the container.

    Args:
        client: Docker client.
        container_id: 12-character container ID prefix.
        pattern: Search pattern (regex for ``lexical``,
            AST pattern for ``structural``).
        path: Directory or file path to search within (default ``"/"``).
        mode: ``"lexical"`` (ripgrep → grep fallback) or
            ``"structural"`` (ast-grep).
        max_results: Maximum results to return (default 50).

    Returns:
        List of dicts with ``file``, ``line`` (int), ``text`` fields.
        Returns ``[{"error": ...}]`` on failure.
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"error": f"Container {container_id[:12]} not found: {e}"}]

    if mode == "structural":
        return _search_structural(container, pattern, path, max_results)
    else:
        return _search_lexical(container, pattern, path, max_results)


def _search_lexical(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Lexical search: ripgrep first, grep fallback."""
    quoted_pattern = shlex.quote(pattern)
    quoted_path = shlex.quote(path)

    cmd = f"rg --json -n {quoted_pattern} {quoted_path} -I 2>/dev/null"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return _grep_fallback(container, pattern, path, max_results)
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return [{"error": f"ripgrep failed (exit {exit_code}): {stderr_text}"}]

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_rg_json(raw, max_results)


def _grep_fallback(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fallback to grep when ripgrep is not available."""
    quoted_pattern = shlex.quote(pattern)
    quoted_path = shlex.quote(path)

    cmd = f"grep -rnI {quoted_pattern} {quoted_path} 2>/dev/null"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return [{"error": "Neither ripgrep (rg) nor grep found in container"}]
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return [{"error": f"grep failed (exit {exit_code}): {stderr_text}"}]

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_grep_output(raw, max_results)


def _search_structural(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Structural search using ast-grep."""
    quoted_pattern = shlex.quote(pattern)
    quoted_path = shlex.quote(path)

    cmd = f"sg run -p {quoted_pattern} {quoted_path} --json=stream 2>/dev/null"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return [{"error": "ast-grep (sg) not found in container"}]
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return [{"error": f"ast-grep failed (exit {exit_code}): {stderr_text}"}]

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_sg_json(raw, max_results)


# ---------------------------------------------------------------------------
# Parser: ripgrep --json output
# ---------------------------------------------------------------------------


def _parse_rg_json(raw: str, max_results: int) -> list[dict[str, Any]]:
    """Parse ripgrep ``--json`` output.

    Each line is a JSON object with a ``type`` field:
    - ``match``: a matching line (fields: ``path.text``,
      ``data.lines.text``, ``data.line_number``)
    - ``begin`` / ``end`` / ``summary``: ignored

    Returns list of ``{file, line, text}`` dicts, capped at *max_results*.
    """
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        file_path = ""
        if "path" in obj.get("data", {}):
            file_path = obj["data"]["path"].get("text", "")
        match_text = obj.get("data", {}).get("lines", {}).get("text", "")
        line_no = obj.get("data", {}).get("line_number", 0)
        results.append(
            {
                "file": file_path,
                "line": int(line_no),
                "text": match_text.rstrip("\n"),
            }
        )
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# Parser: grep output
# ---------------------------------------------------------------------------


def _parse_grep_output(raw: str, max_results: int) -> list[dict[str, Any]]:
    """Parse standard ``grep -rnI`` output (``file:line:text``).

    Returns list of ``{file, line, text}`` dicts, capped at *max_results*.
    """
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _GREP_OUTPUT_RE.match(line)
        if m:
            results.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "text": m.group(3),
                }
            )
            if len(results) >= max_results:
                break
    return results


# ---------------------------------------------------------------------------
# Parser: ast-grep (sg) --json output
# ---------------------------------------------------------------------------


def _parse_sg_json(raw: str, max_results: int) -> list[dict[str, Any]]:
    """Parse ``sg run --json=stream`` output.

    ``sg run --json=stream`` outputs one JSON object per line.
    """
    raw = raw.strip()
    if not raw:
        return []

    results: list[dict[str, Any]] = []
    lines = raw.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries = obj if isinstance(obj, list) else [obj]
        for entry in entries:
            file_path = entry.get("file", "")
            match_range = entry.get("range", {})
            start = match_range.get("start", {})
            line_no = start.get("line", 0)
            text = entry.get("text", "")
            results.append(
                {
                    "file": file_path,
                    "line": int(line_no),
                    "text": text.strip("\n"),
                }
            )
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# Sandbox environment for tools that need writable cache dirs.
# ---------------------------------------------------------------------------

#: Environment variables to set before running linters/type checkers
#: inside sandbox containers.  Containers run as a non-root user with
#: a read-only ``/``, so cache directories must point to ``/tmp``.
_SANDBOX_ENV: str = (
    "RUFF_CACHE_DIR=/tmp/.ruff_cache "
    "MYPY_CACHE_DIR=/tmp/.mypy_cache "
    "mkdir -p /tmp/.ruff_cache /tmp/.mypy_cache 2>/dev/null; "
)


# ---------------------------------------------------------------------------
# Public API: called by @mcp.tool() handlers in server.py
# ---------------------------------------------------------------------------


def apply_patch_to_file(
    client: Any,
    container_id: str,
    file_path: str,
    diff_content: str,
) -> str:
    """Apply a unified diff to a file inside the sandbox container."""
    try:
        current = _read_file(client, container_id, file_path)
    except ValueError as e:
        return f"Error: {e}"

    try:
        patched = apply_unified_diff(current, diff_content)
    except ValueError as e:
        return f"Error: failed to apply diff: {e}"

    try:
        write_file(client, container_id, file_path, patched)
    except ValueError as e:
        return f"Error: {e}"

    return f"Patch applied successfully to {file_path} in container {container_id[:12]}"


def read_file_lines(
    client: Any,
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Read *limit* lines from *file_path* starting at *offset.

    Returns a dict with:
    - ``content`` (str): the requested lines joined by newline
    - ``total_lines`` (int): total number of lines in the file
    - ``shown`` (int): number of lines returned
    - ``has_more`` (bool): whether there are more lines after this range
    - ``next_offset`` (int | None): offset for the next page (if any)
    - ``error`` (str | None): error message if the read failed
    """
    try:
        content = _read_file(client, container_id, file_path)
    except ValueError as e:
        return {"error": str(e)}

    lines = content.split("\n")
    total = len(lines)
    page_lines = lines[offset : offset + limit]
    next_offset = offset + limit
    has_more = next_offset < total

    return {
        "content": "\n".join(page_lines),
        "total_lines": total,
        "shown": len(page_lines),
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "error": None,
    }


def lint_file(
    client: Any,
    container_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a list of dicts, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``, ``"unused-import"``)
    - ``message`` (str): human-readable message

    If no suitable linter is installed in the container, returns a
    single entry with ``rule`` set to ``"no-linter"`` and a
    descriptive message listing the expected tools.

    Supported:
    - ``.py`` files → ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` files → ``eslint``
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]

    ext = _get_extension(file_path)

    if ext in (".py",):
        return _run_python_linter(container, file_path)
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        return _run_js_linter(container, file_path)
    else:
        return [
            {
                "file": file_path,
                "line": 0,
                "rule": "no-linter",
                "message": f"No linter configured for {ext} files",
            }
        ]


def _run_python_linter(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Try ruff, fall back to pylint. Report tool absence clearly."""
    # ruff (primary)
    ruff_result = _run_ruff(container, file_path)
    if ruff_result is not None:
        return ruff_result  # ruff ran (results may be empty = clean file)

    # pylint (fallback)
    pylint_result = _run_pylint(container, file_path)
    if pylint_result is not None:
        return pylint_result

    return [
        {
            "file": file_path,
            "line": 0,
            "rule": "no-linter",
            "message": (
                "No Python linter found in container. "
                "Install ruff or pylint, or use a custom image "
                "(pass --default-image to the server)."
            ),
        }
    ]


def _run_js_linter(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Try eslint."""
    eslint_result = _run_eslint(container, file_path)
    if eslint_result is not None:
        return eslint_result

    return [
        {
            "file": file_path,
            "line": 0,
            "rule": "no-linter",
            "message": (
                "No JS/TS linter found in container. "
                "Install eslint, or use a custom image "
                "(pass --default-image to the server)."
            ),
        }
    ]


def type_check_file(
    client: Any,
    container_id: str,
    file_path: str,
) -> list[dict[str, Any]]:
    """Run a type checker on *file_path* inside the container.

    Returns the same structure as :func:`lint_file`.
    If no type checker is installed, returns ``rule: "no-typechecker"``.

    Supported:
    - ``.py`` files → ``mypy`` (falls back to ``pyright``)
    - ``.ts``, ``.tsx`` files → ``tsc --noEmit``
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]

    ext = _get_extension(file_path)

    if ext in (".py",):
        return _run_python_typecheck(container, file_path)
    elif ext in (".ts", ".tsx"):
        return _run_ts_typecheck(container, file_path)
    else:
        return [
            {
                "file": file_path,
                "line": 0,
                "rule": "no-typechecker",
                "message": f"No type checker configured for {ext} files",
            }
        ]


def _run_python_typecheck(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Try mypy, fall back to pyright."""
    mypy_result = _run_mypy(container, file_path)
    if mypy_result is not None:
        return mypy_result

    pyright_result = _run_pyright(container, file_path)
    if pyright_result is not None:
        return pyright_result

    return [
        {
            "file": file_path,
            "line": 0,
            "rule": "no-typechecker",
            "message": (
                "No Python type checker found in container. "
                "Install mypy or pyright, or use a custom image "
                "(pass --default-image to the server)."
            ),
        }
    ]


def _run_ts_typecheck(container: Any, file_path: str) -> list[dict[str, Any]]:
    """Try tsc."""
    tsc_result = _run_tsc(container, file_path)
    if tsc_result is not None:
        return tsc_result

    return [
        {
            "file": file_path,
            "line": 0,
            "rule": "no-typechecker",
            "message": (
                "No TypeScript type checker found in container. "
                "Install typescript (tsc), or use a custom image "
                "(pass --default-image to the server)."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Extension helper
# ---------------------------------------------------------------------------


def _get_extension(file_path: str) -> str:
    """Return the lowercase file extension including the dot."""
    _, dot_ext = file_path.rstrip("/").rsplit(".", 1) if "." in file_path else ("", "")
    return f".{dot_ext.lower()}" if dot_ext else ""


# ---------------------------------------------------------------------------
# Linter runners
# ---------------------------------------------------------------------------


def _run_ruff(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``ruff check --output-format json``. Returns None if ruff is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json {_quote_path(file_path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    # exit_code 127 = command not found
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_ruff_output(stdout_text, file_path)


def _parse_ruff_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse ruff JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append(
            {
                "file": issue.get("filename", file_path),
                "line": int(issue.get("location", {}).get("row", 0)),
                "rule": issue.get("code", "unknown"),
                "message": issue.get("message", ""),
            }
        )
    return results


def _run_pylint(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``pylint --output-format json``. Returns None if pylint is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pylint --output-format json {_quote_path(file_path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_pylint_output(stdout_text, file_path)


def _parse_pylint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pylint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append(
            {
                "file": issue.get("path", file_path),
                "line": int(issue.get("line", 0)),
                "rule": issue.get("symbol", issue.get("message-id", "unknown")),
                "message": issue.get("message", ""),
            }
        )
    return results


def _run_eslint(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``eslint --format json``. Returns None if eslint is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}eslint --format json {_quote_path(file_path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_eslint_output(stdout_text, file_path)


def _parse_eslint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse eslint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    for result in data:
        fpath = result.get("filePath", file_path)
        for msg in result.get("messages", []):
            results.append(
                {
                    "file": fpath,
                    "line": int(msg.get("line", 0)),
                    "rule": msg.get("ruleId", "unknown"),
                    "message": msg.get("message", ""),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Type checker runners
# ---------------------------------------------------------------------------


def _run_mypy(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``mypy --show-error-codes``. Returns None if mypy is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}mypy --show-error-codes {_quote_path(file_path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_mypy_output(stdout_text, file_path)


#: Regex for mypy output: ``file:line:column: severity: message [error-code]``
_MYPY_LINE_RE = re.compile(
    r"^(.+?):(\d+):\d+:\s*(error|warning|note):\s*(.+?)(?:\s+\[([^\]]+)\])?\s*$"
)


def _parse_mypy_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse mypy text output into the common result format."""
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _MYPY_LINE_RE.match(line)
        if m:
            results.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "rule": m.group(5) or m.group(3),  # error code or severity
                    "message": m.group(4),
                }
            )
    return results


def _run_pyright(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``pyright --outputjson``. Returns None if pyright is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson {_quote_path(file_path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_pyright_output(stdout_text, file_path)


def _parse_pyright_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pyright JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for diag in data.get("generalDiagnostics", []):
        results.append(
            {
                "file": diag.get("file", file_path),
                "line": int(diag.get("range", {}).get("start", {}).get("line", 0)) + 1,
                "rule": diag.get("rule", "unknown"),
                "message": diag.get("message", ""),
            }
        )
    return results


def _run_tsc(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``tsc --noEmit``. Returns None if tsc is not installed."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}npx tsc --noEmit {_quote_path(file_path)} 2>&1 || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return None
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    parsed = _parse_tsc_json(stdout_text, file_path)
    if not parsed:
        parsed = _parse_tsc_text(stdout_text, file_path)
    return parsed


#: Regex for tsc text output: ``file(line,col): error TSXXXX: message``
_TSC_TEXT_RE = re.compile(
    r"^(.+?)\((\d+)(?:,\d+)?\):\s*(error|warning)\s+(TS\d+):\s*(.+)$"
)


def _parse_tsc_text(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc text output into the common result format."""
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _TSC_TEXT_RE.match(line)
        if m:
            results.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "rule": m.group(4),
                    "message": m.group(5),
                }
            )
    return results


def _parse_tsc_json(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc JSON output (``--listFiles`` style) if available."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for diag in data.get("diagnostics", []):
            results.append(
                {
                    "file": diag.get("file", {}).get("fileName", file_path),
                    "line": int(diag.get("file", {}).get("line", 0)),
                    "rule": diag.get("code", "unknown"),
                    "message": diag.get("messageText", ""),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Severity helper for lint rules
# ---------------------------------------------------------------------------

#: Ruff rule code prefixes mapped to severity.
_RUFF_SEVERITY_MAP: dict[str, str] = {
    "E": "error",      # pycodestyle errors
    "F": "error",      # Pyflakes
    "B": "error",      # flake8-bugbear
    "RUF": "error",    # ruff-specific rules
    "W": "warning",    # pycodestyle warnings
    "C90": "warning",  # mccabe complexity
    "N": "warning",    # pep8-naming
    "D": "warning",    # pydocstyle
    "I": "info",       # isort
    "SIM": "info",     # flake8-simplify
    "PL": "info",      # Pylint
    "UP": "info",      # pyupgrade
    "CPY": "info",     # flake8-copyright
    "TID": "info",     # flake8-tidy-imports
    "TCH": "info",     # flake8-type-checking
    "Q": "info",       # flake8-quotes
    "RET": "info",     # flake8-return
    "ARG": "info",     # flake8-unused-arguments
    "PTH": "info",     # flake8-use-pathlib
    "G": "info",       # flake8-logging-format
    "PGH": "info",     # pygrep-hooks
    "S": "warning",    # flake8-bandit (security)
}


def _determine_lint_severity(rule: str) -> str:
    """Map a lint rule code to a severity level.

    Uses rule code prefix matching against
    :data:`_RUFF_SEVERITY_MAP`.  Falls back to ``"error"`` for
    unrecognised codes (conservative default).
    """
    if not rule:
        return "error"
    for prefix, severity in sorted(_RUFF_SEVERITY_MAP.items(),
                                   key=lambda x: -len(x[0])):
        if rule.startswith(prefix):
            return severity
    return "error"


# ---------------------------------------------------------------------------
# Verify: bundled lint + type_check + test + scan   (Issue #54)
# TODO: consider parallel execution with ThreadPoolExecutor for large projects
# ---------------------------------------------------------------------------


def _run_ruff_verify(container: Any, path: str) -> list[dict[str, Any]]:
    """Run ruff on *path* for verify.  Single-tool, no fallback."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json "
            f"{_quote_path(path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return [
            {
                "file": path,
                "line": 0,
                "rule": "no-linter",
                "severity": "info",
                "message": "ruff not installed",
            }
        ]
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    results = _parse_ruff_output(stdout_text, path)
    for r in results:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return results


def _run_pyright_verify(container: Any, path: str) -> list[dict[str, Any]]:
    """Run pyright on *path* for verify.  Single-tool, no fallback."""
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson "
            f"{_quote_path(path)} 2>/dev/null || true",
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return [
            {
                "file": path,
                "line": 0,
                "rule": "no-typechecker",
                "severity": "info",
                "message": "pyright not installed",
            }
        ]
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    results = _parse_pyright_output(stdout_text, path)
    for r in results:
        r["severity"] = "error"
    return results


def _run_pytest_verify(container: Any, path: str) -> dict[str, Any]:
    """Run pytest with json-report on *path*.

    Returns a dict with keys ``status``, ``passed``, ``failed``,
    ``duration``, and optionally ``failures``, matching
    :class:`code_sandbox_mcp.test_report.TestReport.to_dict`.
    """
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            (
                f"python3 -m pytest --json-report "
                f"--json-report-file=- {_quote_path(path)} 2>/dev/null || true"
            ),
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return {"status": "skipped", "message": "python3 not found"}
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return {"status": "skipped", "message": "no test output"}

    try:
        from code_sandbox_mcp.test_report import PytestAdapter

        report = PytestAdapter.parse_json(stdout_text)
        return report.to_dict()
    except Exception:
        return {
            "status": "skipped",
            "message": "pytest not installed or no tests found",
        }


def _run_semgrep_verify(container: Any, path: str) -> list[dict[str, Any]]:
    """Run semgrep scan on *path* for verify.

    Uses Python and security-audit rule sets.
    """
    exit_code, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            (
                f"semgrep scan --config p/python --config p/security-audit "
                f"--json {_quote_path(path)} 2>/dev/null || true"
            ),
        ],
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return [
            {
                "file": path,
                "line": 0,
                "rule": "no-scanner",
                "severity": "info",
                "message": "semgrep not installed",
            }
        ]
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_semgrep_output(stdout_text, path)


def _parse_semgrep_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse ``semgrep scan --json`` output into the common format.

    Each result includes ``severity`` (``ERROR`` / ``WARNING`` / ``INFO``)
    directly from semgrep's output.
    """
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for finding in data.get("results", []):
        results.append(
            {
                "file": finding.get("path", file_path),
                "line": int(finding.get("start", {}).get("line", 0)),
                "rule": finding.get("check_id", "unknown"),
                "severity": finding.get("extra", {}).get("severity", "WARNING"),
                "message": finding.get("extra", {}).get("message", ""),
            }
        )
    return results


def run_verify(
    client: Any,
    container_id: str,
    path: str,
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
) -> dict[str, Any]:
    """Run lint + type_check + test + scan and return unified results.

    This is the core of the Issue #54 verify tool.  It bundles four
    analysis layers into a single call, normalises output, and
    computes a gate decision.

    Args:
        client: Docker client.
        container_id: 12-character container ID prefix.
        path: File or directory path inside the container.
        gate_on_lint_error: Whether lint errors fail the gate
            (default ``True``).
        gate_on_type_error: Whether type-check errors fail the gate
            (default ``False``).
        gate_on_test_fail: Whether test failures fail the gate
            (default ``True``).
        gate_on_scan_error: Whether semgrep ERROR findings fail the gate
            (default ``True``).
        gate_on_scan_warning: Whether semgrep WARNING findings fail the gate
            (default ``False``).

    Returns:
        A dict with:

        - ``status``: ``"ok"`` or ``"failed"``
        - ``gate_passed``: ``True`` if all gate conditions are satisfied
        - ``lint``: list of lint findings
        - ``types``: list of type-check findings
        - ``tests``: test report dict
        - ``scan``: list of semgrep findings
        - ``gate_fail_reasons`` (optional): list of human-readable reasons
          why the gate failed
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return {
            "status": "error",
            "gate_passed": False,
            "error": f"Container {container_id[:12]} not found: {e}",
        }

    lint_results = _run_ruff_verify(container, path)
    type_results = _run_pyright_verify(container, path)
    test_results = _run_pytest_verify(container, path)
    scan_results = _run_semgrep_verify(container, path)

    gate_fail_reasons: list[str] = []

    if gate_on_lint_error:
        lint_errors = [
            r for r in lint_results
            if r.get("severity") == "error"
            # "error" rule is a safety net for unexpected paths;
            # _run_ruff_verify never produces it today.
            and r.get("rule") not in ("no-linter", "error")
        ]
        if lint_errors:
            gate_fail_reasons.append(
                f"lint: {len(lint_errors)} error(s)"
            )

    if gate_on_type_error:
        type_errors = [
            r for r in type_results
            if r.get("severity") == "error"
            and r.get("rule") not in ("no-typechecker", "error")
        ]
        if type_errors:
            gate_fail_reasons.append(
                f"type_check: {len(type_errors)} error(s)"
            )

    if gate_on_test_fail:
        if test_results.get("status") == "failed":
            gate_fail_reasons.append(
                f"tests: {test_results.get('failed', 0)} failure(s)"
            )

    if gate_on_scan_error:
        scan_errors = [
            r for r in scan_results
            if r.get("severity") == "ERROR"
            and r.get("rule") not in ("no-scanner",)
        ]
        if scan_errors:
            gate_fail_reasons.append(
                f"scan: {len(scan_errors)} ERROR(s)"
            )

    if gate_on_scan_warning:
        scan_warnings = [
            r for r in scan_results
            if r.get("severity") == "WARNING"
        ]
        if scan_warnings:
            gate_fail_reasons.append(
                f"scan: {len(scan_warnings)} WARNING(s)"
            )

    gate_passed = len(gate_fail_reasons) == 0
    overall_status = "failed" if not gate_passed else "ok"

    result: dict[str, Any] = {
        "status": overall_status,
        "gate_passed": gate_passed,
        "lint": lint_results,
        "types": type_results,
        "tests": test_results,
        "scan": scan_results,
    }
    if gate_fail_reasons:
        result["gate_fail_reasons"] = gate_fail_reasons

    return result
