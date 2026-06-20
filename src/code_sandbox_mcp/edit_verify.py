"""Edit/Verify subsystem: minimal edit loop primitives for sandbox containers.

Provides low-level file editing and verification tools that operate on
disposable sandbox containers (not the real repository).  These tools
form the core of the minimal edit loop:

    search_in_container -> read_file_range -> apply_patch
    -> lint/type_check -> rerun_failed

By sending only diffs and reading only the needed lines, each iteration
consumes only hundreds of tokens instead of thousands.

Supports multi-language verification (Python / JS / TS / Go) with
language-aware dispatch, status envelopes, and proper gate logic.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import fnmatch
from dataclasses import dataclass, field
from typing import Any

from code_sandbox_mcp.journal import record_file_write


# ===========================================================================
# Status envelope (design-multilang-support.md S4)
# ===========================================================================


@dataclass
class VerifyResult:
    """Status envelope for a single verification layer.

    Each runner (lint / type / test / scan) returns one VerifyResult
    instead of a bare list of findings, so that errors, missing tools,
    and intentional skips are never silently treated as "clean".
    """

    tool: str
    status: str  # "ok" | "findings" | "not_available" | "error" | "skipped"
    findings: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    exit_code: int = -1


def _envelope_ok(tool: str, findings: list[dict[str, Any]] = None, exit_code: int = 0) -> VerifyResult:
    if findings is None:
        findings = []
    return VerifyResult(
        tool=tool,
        status="findings" if findings else "ok",
        findings=findings,
        exit_code=exit_code,
    )


def _envelope_not_available(tool: str, detail: str = "") -> VerifyResult:
    return VerifyResult(
        tool=tool, status="not_available", detail=detail, exit_code=127,
    )


def _envelope_error(tool: str, detail: str, exit_code: int) -> VerifyResult:
    return VerifyResult(
        tool=tool, status="error", detail=detail, exit_code=exit_code,
    )


def _envelope_skipped(tool: str, reason: str) -> VerifyResult:
    return VerifyResult(
        tool=tool, status="skipped", detail=reason,
    )


@dataclass
class DetectionResult:
    """Result of language detection with scope information.

    Attributes:
        languages: Detected language set (e.g. {"python"}, {"python", "js"}, set()).
        scope: Language to root path mapping for polyglot projects
               (e.g. {"python": "backend/", "js": "frontend/"}).
        reason: Human-readable explanation when languages is empty (unknown).
    """

    languages: set[str]
    scope: dict[str, str]
    reason: str | None = None


# ===========================================================================
# Language detection (design-multilang-support.md S3)
# ===========================================================================

_LANGUAGE_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "ts",
    ".tsx": "ts",
    ".go": "go",
}

# (pattern, language) — pattern supports fnmatch glob (e.g. requirements*.txt)
_DETECTION_MARKERS: list[tuple[str, str]] = [
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements*.txt", "python"),
    ("Pipfile", "python"),
    ("tox.ini", "python"),
    ("package.json", "js"),
    ("tsconfig.json", "ts"),
]

_EXCLUDE_DIRS: tuple[str, ...] = (
    "node_modules", ".venv", "vendor", "dist", "build",
)


def detect_languages(
    container: Any,
    path: str,
    language: str | None = None,
) -> DetectionResult:
    """Detect languages from a file or directory path inside the container.

    Priority:
    1. Explicit ``language=`` parameter (skip detection).
    2. File extension map, with tsconfig.json upward search for .ts files.
    3. Directory marker files and glob patterns (e.g. ``requirements*.txt``)
       via a single ``find`` exec for efficiency.

    For polyglot projects, returns a ``scope`` dict mapping each language
    to its root directory so tools can be run per sub-tree.

    Returns:
        ``DetectionResult(languages, scope, reason)`` where *reason* is
        set when languages are empty (unknown).
    """
    if language:
        return DetectionResult(languages={language}, scope={language: path})

    ext = _get_extension(path)
    if ext in _LANGUAGE_EXT_MAP:
        lang = _LANGUAGE_EXT_MAP[ext]
        scope_dir = path
        if lang == "ts":
            tsconfig_dir = _find_tsconfig_upward(container, path)
            if tsconfig_dir is not None:
                scope_dir = tsconfig_dir
        return DetectionResult(languages={lang}, scope={lang: scope_dir})

    lang_scope: dict[str, str] = {}
    find_expr_parts = []
    for pattern, _ in _DETECTION_MARKERS:
        find_expr_parts.append(f'-name "{pattern}"')
    or_expr = " -o ".join(find_expr_parts)
    find_cmd = f"find {path} -maxdepth 1 \\( {or_expr} \\) 2>/dev/null"

    ec, output = container.exec_run(
        ["/bin/sh", "-c", find_cmd],
        stdout=True,
        stderr=True,
    )
    if ec == 0:
        stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
        out = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        for line_out in out.strip().split("\n"):
            line_out = line_out.strip()
            if not line_out:
                continue
            basename = os.path.basename(line_out)
            marker_dir = os.path.dirname(line_out)
            for pattern, marker_lang in _DETECTION_MARKERS:
                if fnmatch.fnmatch(basename, pattern):
                    lang_scope[marker_lang] = marker_dir
                    break

    if not lang_scope:
        return DetectionResult(
            languages=set(),
            scope={},
            reason=(
                "No recognized project markers found in path. "
                "Use language= parameter to force a specific toolchain."
            ),
        )

    languages = set(lang_scope.keys())
    return DetectionResult(languages=languages, scope=lang_scope)


def _find_tsconfig_upward(container: Any, file_path: str) -> str | None:
    """Search upward from *file_path* for a tsconfig.json.

    Returns the directory containing tsconfig.json, or None if not found.
    """
    current = os.path.dirname(os.path.abspath(file_path))
    while True:
        ec, output = container.exec_run(
            ["/bin/sh", "-c", f"test -f {current}/tsconfig.json && echo found || echo notfound"],
            stdout=True,
            stderr=True,
        )
        stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
        out = stdout_part.decode("utf-8", errors="replace").strip() if stdout_part else ""
        if "found" in out:
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


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
        return content  # Empty diff -> no change

    hunks = _parse_hunks(diff_text)
    if not hunks:
        return content  # No hunks -> no change

    original_ends_with_newline = content.endswith("\n")
    lines = content.split("\n")

    # When the original content ends with \n, split() produces a trailing
    # empty element (e.g. "a\nb\n" -> ["a", "b", ""]).  Removing it here
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


def write_file(container: Any, container_id_short: str, file_path: str, content: str) -> None:
    """Write *content* to *file_path* in the sandbox container.

    Ensures the parent directory exists and records the write
    in the execution journal (Issue #96).
    """
    if not file_path.startswith("/"):
        raise ValueError(f"file_path must be absolute: {file_path!r}")
    canon = os.path.normpath(file_path)
    if ".." in canon.split(os.sep):
        raise ValueError(f"Path traversal detected: {file_path!r}")

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
        container_id_short,
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
        mode: ``"lexical"`` (ripgrep -> grep fallback) or
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
        container = client.containers.get(container_id)
    except Exception as e:
        return f"Error: Container {container_id[:12]} not found: {e}"

    try:
        current = read_file(container, file_path)
    except ValueError as e:
        return f"Error: {e}"

    try:
        patched = apply_unified_diff(current, diff_content)
    except ValueError as e:
        return f"Error: failed to apply diff: {e}"

    try:
        write_file(container, container_id[:12], file_path, patched)
    except ValueError as e:
        return f"Error: {e}"

    return f"Patch applied successfully to {file_path} in container {container_id[:12]}"


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
        "error": None,
    }


# ---------------------------------------------------------------------------
# Extension helper
# ---------------------------------------------------------------------------


def _get_extension(file_path: str) -> str:
    """Return the lowercase file extension including the dot."""
    _, dot_ext = file_path.rstrip("/").rsplit(".", 1) if "." in file_path else ("", "")
    return f".{dot_ext.lower()}" if dot_ext else ""


# ---------------------------------------------------------------------------
# Linter / Type checker / Test / Scan runners
# ---------------------------------------------------------------------------
# Each runner now returns a VerifyResult envelope.  The ``|| true`` and
# ``2>/dev/null`` silencing has been removed: exit codes are inspected
# directly, and stderr is captured (not discarded).
#
# Runner return semantics:
# - exit 0   + output -> status "findings" (parse output)
# - exit 0   + no output -> status "ok" (clean)
# - exit 1   (many tools use this for "findings") -> status "findings"
# - exit 127             -> status "not_available"
# - exit other           -> status "error" (unexpected failure)
# - "skipped" is only for intentional non-execution (e.g. go type layer)


def _run_ruff_verify(container: Any, path: str) -> VerifyResult:
    """Run ruff on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("ruff", "ruff not installed in container")
    if ec not in (0, 1):
        return _envelope_error("ruff", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_ruff_output(stdout_text, path)
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _envelope_ok("ruff", findings, ec)


def _run_eslint_verify(container: Any, path: str) -> VerifyResult:
    """Run eslint on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}eslint --format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("eslint", "eslint not installed in container")
    if ec not in (0, 1, 2):
        # eslint exit 2 = runtime error
        return _envelope_error("eslint", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_eslint_output(stdout_text, path)
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _envelope_ok("eslint", findings, ec)


def _run_golangci_lint_verify(container: Any, path: str) -> VerifyResult:
    """Run golangci-lint on *path*.  Falls back to go vet."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}golangci-lint run --out-format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    if ec == 127:
        return _run_go_vet_verify(container, path)

    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec not in (0, 1):
        # golangci-lint uses exit 2 for execution errors (config issues, etc.)
        return _envelope_error("golangci-lint", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_golangci_lint_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("golangci-lint", findings, ec)


def _run_go_vet_verify(container: Any, path: str) -> VerifyResult:
    """Run go vet on *path*."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}go vet {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("go vet", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go vet", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_go_vet_output(stdout_text + "\n" + stderr_text, path)
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("go vet", findings, ec)


def _parse_golangci_lint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse golangci-lint JSON output (when available)."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for issue in data.get("Issues", []):
            pos = issue.get("Pos", {})
            results.append({
                "file": pos.get("Filename", ""),
                "line": int(pos.get("Line", 0)),
                "rule": issue.get("FromLinter", "unknown"),
                "message": issue.get("Text", ""),
            })
    return results


def _parse_go_vet_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse go vet text output (file:line:col: message)."""
    results: list[dict[str, Any]] = []
    pat = re.compile(r"^(.+?):(\d+):\d+:\s*(.+)$")
    for line in raw.split("\n"):
        m = pat.match(line.strip())
        if m:
            results.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "rule": "go-vet",
                "message": m.group(3),
            })
    return results


def _run_pyright_verify(container: Any, path: str) -> VerifyResult:
    """Run pyright on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pyright", "pyright not installed in container")
    if ec not in (0, 1):
        return _envelope_error("pyright", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_pyright_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("pyright", findings, ec)


def _run_mypy_verify(container: Any, path: str) -> VerifyResult:
    """Run mypy on *path* (fallback for pyright).  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}mypy --show-error-codes {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("mypy", "mypy not installed in container")
    if ec not in (0, 1):
        return _envelope_error("mypy", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_mypy_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("mypy", findings, ec)


def _run_tsc_verify(container: Any, path: str) -> VerifyResult:
    """Run tsc --noEmit on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}npx tsc --noEmit {_quote_path(path)} 2>&1",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    combined = ""
    if stdout_part:
        combined += stdout_part.decode("utf-8", errors="replace")
    if stderr_part:
        combined += stderr_part.decode("utf-8", errors="replace")

    if ec == 127:
        return _envelope_not_available("tsc", "typescript (tsc) not installed in container")
    if ec not in (0, 1, 2):
        return _envelope_error("tsc", combined.strip() or f"exit code {ec}", ec)

    findings = _parse_tsc_text(combined, path)
    if not findings:
        findings = _parse_tsc_json(combined, path)
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("tsc", findings, ec)


def _run_pytest_verify(container: Any, path: str) -> VerifyResult:
    """Run pytest --json-report on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}python3 -m pytest --json-report --json-report-file=- {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pytest", "python3 not found in container")
    if ec == 5:
        # pytest exit 5 = no tests collected
        return _envelope_skipped("pytest", "no tests found")
    if ec not in (0, 1):
        return _envelope_error("pytest", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _envelope_skipped("pytest", "no test output produced")

    try:
        from code_sandbox_mcp.test_report import PytestAdapter

        report = PytestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="pytest",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        return _envelope_error(
            "pytest", "failed to parse pytest output", ec,
        )


def _run_jest_verify(container: Any, path: str) -> VerifyResult:
    """Run jest --json on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}npx jest --json --passWithNoTests {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("jest", "jest not installed in container")
    if ec not in (0, 1):
        return _envelope_error("jest", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _envelope_skipped("jest", "no test output produced")

    try:
        from code_sandbox_mcp.test_report import JestAdapter

        report = JestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="jest",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        return _envelope_error(
            "jest", "failed to parse jest output", ec,
        )


def _run_go_test_verify(container: Any, path: str) -> VerifyResult:
    """Run go test -json on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}go test -json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("go test", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go test", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _envelope_skipped("go test", "no test output produced")

    try:
        from code_sandbox_mcp.test_report import GoTestAdapter

        report = GoTestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="go test",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        return _envelope_error(
            "go test", "failed to parse go test output", ec,
        )


def _run_semgrep_verify(container: Any, path: str, lang_config: str = "p/python") -> VerifyResult:
    """Run semgrep scan on *path* with language-specific config."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}semgrep scan --config {lang_config} --config p/security-audit "
            f"--json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("semgrep", "semgrep not installed in container")
    if ec not in (0, 1, 2):
        # semgrep exit 2 = findings found (normal)
        return _envelope_error("semgrep", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_semgrep_output(stdout_text, path)
    if not findings and stdout_text.strip() and ec >= 2:
        return _envelope_error(
            "semgrep",
            f"parse error: semgrep output was non-empty but could not be parsed (exit {ec})",
            ec,
        )
    return _envelope_ok("semgrep", findings, ec)


# ---------------------------------------------------------------------------
# Unified dispatch table
# ---------------------------------------------------------------------------
# Maps language -> layer -> runner function.
# Python type layer tries pyright first, falls back to mypy.
# Go lint tries golangci-lint first, falls back to go vet.
# JS has no type layer (skipped).  Go type is covered by go vet/build.


_DISPATCH: dict[str, dict[str, Any]] = {
    "python": {
        "lint": _run_ruff_verify,
        "type": _run_pyright_verify,  # primary
        "type_fallback": _run_mypy_verify,
        "test": _run_pytest_verify,
        "scan": lambda c, p: _run_semgrep_verify(c, p, "p/python"),
    },
    "js": {
        "lint": _run_eslint_verify,
        "type": None,  # skipped
        "type_fallback": None,
        "test": _run_jest_verify,
        "scan": lambda c, p: _run_semgrep_verify(c, p, "p/javascript"),
    },
    "ts": {
        "lint": _run_eslint_verify,
        "type": _run_tsc_verify,
        "type_fallback": None,
        "test": _run_jest_verify,
        "scan": lambda c, p: _run_semgrep_verify(c, p, "p/typescript"),
    },
    "go": {
        "lint": _run_golangci_lint_verify,
        "type": None,  # skipped: build/vet covers typing
        "type_fallback": None,
        "test": _run_go_test_verify,
        "scan": lambda c, p: _run_semgrep_verify(c, p, "p/go"),
    },
    "unknown": {
        "lint": None,
        "type": None,
        "type_fallback": None,
        "test": None,
        "scan": None,
    },
}


# ---------------------------------------------------------------------------
# lint_file / type_check_file (single-file, backward-compatible)
# ---------------------------------------------------------------------------


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
    - ``.py`` files -> ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` files -> ``eslint``
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
    result = _run_ruff_verify(container, file_path)
    if result.status not in ("not_available", "error"):
        return result.findings

    # ruff not available, try pylint
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
    result = _run_eslint_verify(container, file_path)
    if result.status not in ("not_available", "error"):
        return result.findings

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
    - ``.py`` files -> ``pyright`` (falls back to ``mypy``)
    - ``.ts``, ``.tsx`` files -> ``tsc --noEmit``
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
    """Try pyright, fall back to mypy. Matches _DISPATCH priority."""
    # Try pyright first (primary, matches _DISPATCH)
    pyright_result = _run_pyright_verify(container, file_path)
    if pyright_result.status not in ("not_available", "error"):
        return pyright_result.findings

    # Fall back to mypy
    mypy_result = _run_mypy_verify(container, file_path)
    if mypy_result.status not in ("not_available", "error"):
        return mypy_result.findings

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
    """Try tsc. Uses unified runner."""
    tsc_result = _run_tsc_verify(container, file_path)
    if tsc_result.status not in ("not_available", "error"):
        return tsc_result.findings

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
# Legacy single-tool runners (kept for backward compat with old callers)
# ---------------------------------------------------------------------------


def _run_ruff(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``ruff check --output-format json``. Returns None if ruff is not installed."""
    result = _run_ruff_verify(container, file_path)
    if result.status == "not_available":
        return None
    return result.findings


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


def _run_eslint(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``eslint --format json``. Returns None if eslint is not installed."""
    result = _run_eslint_verify(container, file_path)
    if result.status == "not_available":
        return None
    return result.findings


def _run_mypy(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``mypy --show-error-codes``. Returns None if mypy is not installed."""
    result = _run_mypy_verify(container, file_path)
    if result.status == "not_available":
        return None
    return result.findings


def _run_pyright(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``pyright --outputjson``. Returns None if pyright is not installed."""
    result = _run_pyright_verify(container, file_path)
    if result.status == "not_available":
        return None
    return result.findings


def _run_tsc(container: Any, file_path: str) -> list[dict[str, Any]] | None:
    """Run ``tsc --noEmit``. Returns None if tsc is not installed."""
    result = _run_tsc_verify(container, file_path)
    if result.status == "not_available":
        return None
    return result.findings


# ---------------------------------------------------------------------------
# Parsers (unchanged)
# ---------------------------------------------------------------------------


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
# Language-layer dispatch for verify
# ---------------------------------------------------------------------------


def _dispatch_layer(
    container: Any,
    path: str,
    language: str,
    layer: str,
) -> VerifyResult:
    """Run a single verification layer for a given language.

    Returns a VerifyResult envelope, including ``skipped`` for
    languages that don't have a given layer (e.g. JS type checking).
    """
    entry = _DISPATCH.get(language, _DISPATCH["unknown"])
    runner = entry.get(layer)
    if runner is None:
        if language == "unknown":
            return _envelope_skipped(
                f"{language}-{layer}",
                f"language '{language}' has no verification layers",
            )
        return _envelope_skipped(
            f"{language}-{layer}",
            f"language '{language}' has no {layer} layer",
        )

    result = runner(container, path)

    # For type layer: try fallback if primary failed with not_available
    if layer == "type" and result.status == "not_available":
        fallback = entry.get("type_fallback")
        if fallback is not None:
            result = fallback(container, path)

    return result


# ---------------------------------------------------------------------------
# verify: bundled lint + type_check + test + scan  (Issue #54)
# ---------------------------------------------------------------------------


def run_verify(
    client: Any,
    container_id: str,
    path: str,
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
    language: str | None = None,
) -> dict[str, Any]:
    """Run lint + type_check + test + scan with language-aware dispatch.

    Detects project languages from *path* (or uses explicit *language=*
    parameter), dispatches each verification layer to the appropriate
    tool, and computes a gate decision based on the findings.

    Each layer returns a status envelope with one of:
    - ``"ok"`` — ran, no findings
    - ``"findings"`` — ran, findings present
    - ``"not_available"`` — tool not installed in container
    - ``"error"`` — tool ran but failed (unexpected exit, parse error)
    - ``"skipped"`` — intentionally not run (no layer for this language)

    Gate logic:
    - **strict** (submit): any ``"not_available"`` or ``"error"``
      status causes ``gate_passed=false`` with reason
      ``"verification incomplete: <tool> <status>"``.
    - **lenient** (interactive verify): passes but returns
      ``incomplete: true``.

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
        language: Explicit language override (``"python"``, ``"js"``,
            ``"ts"``, ``"go"``).  Skips auto-detection.

    Returns:
        A dict with:
        - ``status``: ``"ok"`` or ``"failed"``
        - ``gate_passed``: ``True`` if all gate conditions are satisfied
        - ``incomplete``: ``True`` if any layer was not available / errored
        - ``detected_languages``: list of detected language keys
        - ``layers``: dict of ``{layer: {language: VerifyResult}}``
        - ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return {
            "status": "error",
            "gate_passed": False,
            "error": f"Container {container_id[:12]} not found: {e}",
        }

    # --- Language detection ---
    detected = detect_languages(container, path, language)

    # --- Run all layers for all detected languages ---
    layers: dict[str, list[VerifyResult]] = {
        "lint": [],
        "type": [],
        "test": [],
        "scan": [],
    }

    for lang in sorted(detected.languages):
        scope_path = detected.scope.get(lang, path)
        for layer_name in ("lint", "type", "test", "scan"):
            vr = _dispatch_layer(container, scope_path, lang, layer_name)
            layers[layer_name].append(vr)

    # --- Gate logic ---
    gate_fail_reasons: list[str] = []
    incomplete = False

    for layer_name, results in layers.items():
        for vr in results:
            if vr.status in ("not_available", "error"):
                incomplete = True
                # strict gate: verification incomplete -> fail
                gate_fail_reasons.append(
                    f"verification incomplete: {vr.tool} {vr.status}"
                    + (f" ({vr.detail})" if vr.detail else "")
                )

    # Lint error gate
    if gate_on_lint_error:
        for vr in layers["lint"]:
            if vr.status == "findings":
                lint_errors = [
                    r for r in vr.findings
                    if r.get("severity") == "error"
                    and r.get("rule") not in ("no-linter", "error")
                ]
                if lint_errors:
                    gate_fail_reasons.append(
                        f"lint ({vr.tool}): {len(lint_errors)} error(s)"
                    )

    # Type error gate
    if gate_on_type_error:
        for vr in layers["type"]:
            if vr.status == "findings":
                type_errors = [
                    r for r in vr.findings
                    if r.get("severity") == "error"
                    and r.get("rule") not in ("no-typechecker", "error")
                ]
                if type_errors:
                    gate_fail_reasons.append(
                        f"type_check ({vr.tool}): {len(type_errors)} error(s)"
                    )

    # Test failure gate
    if gate_on_test_fail:
        for vr in layers["test"]:
            if vr.detail:
                try:
                    test_report = json.loads(vr.detail)
                    if test_report.get("status") == "failed":
                        gate_fail_reasons.append(
                            f"tests ({vr.tool}): "
                            f"{test_report.get('failed', 0)} failure(s)"
                        )
                except (json.JSONDecodeError, ValueError):
                    pass

    # Scan error gate
    if gate_on_scan_error:
        for vr in layers["scan"]:
            if vr.status == "findings":
                scan_errors = [
                    r for r in vr.findings
                    if r.get("severity") == "ERROR"
                    and r.get("rule") not in ("no-scanner",)
                ]
                if scan_errors:
                    gate_fail_reasons.append(
                        f"scan ({vr.tool}): {len(scan_errors)} ERROR(s)"
                    )

    # Scan warning gate
    if gate_on_scan_warning:
        for vr in layers["scan"]:
            if vr.status == "findings":
                scan_warnings = [
                    r for r in vr.findings
                    if r.get("severity") == "WARNING"
                ]
                if scan_warnings:
                    gate_fail_reasons.append(
                        f"scan ({vr.tool}): {len(scan_warnings)} WARNING(s)"
                    )

    gate_passed = len(gate_fail_reasons) == 0
    overall_status = "failed" if not gate_passed else "ok"

    # --- Build result ---
    # Flatten layers for backward compatibility with existing consumers
    result: dict[str, Any] = {
        "status": overall_status,
        "gate_passed": gate_passed,
        "detected_languages": sorted(detected),
        "incomplete": incomplete,
        "lint": _flatten_layer(layers["lint"]),
        "types": _flatten_layer(layers["type"]),
        "tests": _flatten_test_layer(layers["test"]),
        "scan": _flatten_layer(layers["scan"]),
    }

    if detection_warning:
        result["detection_warning"] = detection_warning
    if gate_fail_reasons:
        result["gate_fail_reasons"] = gate_fail_reasons

    return result


def _flatten_layer(results: list[VerifyResult]) -> list[dict[str, Any]]:
    """Flatten a list of VerifyResults into a single findings list.

    For backward compatibility: existing consumers expect
    ``lint`` / ``types`` / ``scan`` to be a flat list of findings.
    """
    all_findings: list[dict[str, Any]] = []
    for vr in results:
        all_findings.extend(vr.findings)
    return all_findings


def _flatten_test_layer(results: list[VerifyResult]) -> dict[str, Any]:
    """Flatten test VerifyResults into a compatible dict.

    For backward compat: existing consumers expect ``tests`` to be
    a dict with ``status``, ``passed``, ``failed``, etc.
    """
    if not results:
        return {"status": "skipped", "message": "no test runner assigned"}

    # Merge multiple test results (polyglot)
    merged: dict[str, Any] = {"status": "ok", "passed": 0, "failed": 0, "duration": 0.0}
    any_run = False
    for vr in results:
        if vr.status in ("skipped", "not_available"):
            continue
        any_run = True
        if vr.detail:
            try:
                tr = json.loads(vr.detail)
            except (json.JSONDecodeError, ValueError):
                continue
            merged["passed"] = merged.get("passed", 0) + tr.get("passed", 0)
            merged["failed"] = merged.get("failed", 0) + tr.get("failed", 0)
            merged["duration"] = merged.get("duration", 0) + tr.get("duration", 0)
            if tr.get("status") == "failed":
                merged["status"] = "failed"
            if "failures" in tr:
                merged.setdefault("failures", []).extend(tr["failures"])

    if not any_run:
        return {"status": "skipped", "message": "no test output"}

    return merged


# ---------------------------------------------------------------------------
# Semgrep output parser
# ---------------------------------------------------------------------------


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
