"""Search functionality for sandbox containers.

Lexical (ripgrep/grep) and structural (ast-grep) search operations for
sandbox containers, extracted from edit_verify.py.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Regex for standard grep output: ``file:line:text``
_GREP_OUTPUT_RE = re.compile(r"^([^:]+):(\d+):(.*)$")


def _build_search_result(
    matches: list[dict[str, Any]],
    max_results: int,
    total: int | None = None,
) -> dict[str, Any]:
    """Build search result dict with metadata."""
    shown = len(matches)
    total_count = total if total is not None else shown
    truncated = total_count > max_results
    result: dict[str, Any] = {
        "matches": matches[:max_results],
        "shown": shown if not truncated else max_results,
        "total": total_count,
        "truncated": truncated,
    }
    if truncated:
        result["next_offset"] = max_results
    return result


def search_files(
    client: Any,
    container_id: str,
    pattern: str,
    path: str = "/",
    mode: str = "lexical",
    max_results: int = 50,
    glob: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
    offset: int = 0,
) -> dict[str, Any]:
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
        glob: Optional glob pattern to filter files (e.g. ``"*.py"``).
        ignore_case: Case-insensitive search (default False).
        context: Number of context lines before/after match (default 0).
        output_mode: ``"content"`` (default), ``"files_with_matches"``,
            or ``"count"``.
        offset: Line offset for pagination (default 0).

    Returns:
        Dict with ``matches`` (list), ``shown``, ``total``, ``truncated``,
        and optionally ``next_offset``.
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return {"status": "error", "error": f"Container {container_id[:12]} not found: {e}"}

    if mode == "structural":
        return _search_structural(container, pattern, path, max_results)
    return _search_lexical(
        container, pattern, path, max_results,
        glob=glob, ignore_case=ignore_case, context=context,
        output_mode=output_mode, offset=offset,
    )


def _needs_pcre2(pattern: str) -> bool:
    """Check if the regex pattern requires PCRE2 (look-around)."""
    return bool(re.search(r'\(\?(?:[=!]|<=|<!)', pattern))


def _build_rg_args(
    pattern: str,
    path: str,
    max_results: int,
    glob: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
    offset: int = 0,
) -> list[str]:
    """Build ripgrep argument list."""
    args = ["rg", "-n"]
    if output_mode == "content":
        args.append("--json")
    elif output_mode == "count":
        args.append("--count-matches")
    elif output_mode == "files_with_matches":
        args.append("--files-with-matches")
    if ignore_case:
        args.append("-i")
    if glob:
        args.extend(["-g", glob])
    if context > 0:
        args.extend(["-C", str(context)])
    if max_results:
        if output_mode == "files_with_matches":
            args.extend(["-m", "1"])
        else:
            args.extend(["-m", str(max_results + 1)])
    if _needs_pcre2(pattern):
        args.append("-P")
    args.append(pattern)
    args.append(path)
    return args


def _build_grep_args(
    pattern: str,
    path: str,
    max_results: int,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
) -> list[str]:
    """Build grep argument list (limited feature set)."""
    args = ["grep", "-rnI"]
    if ignore_case:
        args.append("-i")
    if context > 0:
        args.extend(["-C", str(context)])
    if output_mode == "count":
        args.append("-c")
    elif output_mode == "files_with_matches":
        args.append("-l")
    if max_results:
        args.extend(["-m", str(max_results + 1)])
    args.append(pattern)
    args.append(path)
    return args


def _search_lexical(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
    glob: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
    offset: int = 0,
) -> dict[str, Any]:
    """Lexical search: ripgrep first, grep fallback."""
    args = _build_rg_args(
        pattern, path, max_results,
        glob=glob, ignore_case=ignore_case,
        context=context, output_mode=output_mode, offset=offset,
    )
    exit_code, output = container.exec_run(
        args,
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return _grep_fallback(
            container, pattern, path, max_results,
            ignore_case=ignore_case, context=context,
            output_mode=output_mode,
        )
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return {"status": "error", "error": f"ripgrep failed (exit {exit_code}): {stderr_text}"}

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    if output_mode == "content":
        return _parse_rg_json(raw, max_results)
    elif output_mode == "count":
        return _parse_rg_count_output(raw, max_results)
    return _parse_rg_files_output(raw, max_results)


def _grep_fallback(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
) -> dict[str, Any]:
    """Fallback to grep when ripgrep is not available."""
    args = _build_grep_args(
        pattern, path, max_results,
        ignore_case=ignore_case, context=context, output_mode=output_mode,
    )
    exit_code, output = container.exec_run(
        args,
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return {"status": "error", "error": "Neither ripgrep (rg) nor grep found in container"}
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return {"status": "error", "error": f"grep failed (exit {exit_code}): {stderr_text}"}

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    if output_mode == "content":
        return _parse_grep_output(raw, max_results)
    elif output_mode == "count":
        return _parse_grep_count_output(raw, max_results)
    return _parse_grep_files_output(raw, max_results)


def _search_structural(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
) -> dict[str, Any]:
    """Structural search using ast-grep."""
    args = ["sg", "run", "-p", pattern, path, "--json=stream"]
    exit_code, output = container.exec_run(
        args,
        stdout=True,
        stderr=True,
    )
    if exit_code == 127:
        return {"status": "error", "error": "ast-grep (sg) not found in container"}
    if exit_code not in (0, 1):
        stdout_part, stderr_part = (
            output if isinstance(output, tuple) else (output, b"")
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
        return {"status": "error", "error": f"ast-grep failed (exit {exit_code}): {stderr_text}"}

    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    return _parse_sg_json(raw, max_results)


# ---------------------------------------------------------------------------
# Parser: ripgrep --json output
# ---------------------------------------------------------------------------


def _parse_rg_json(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ripgrep ``--json`` output.

    Each line is a JSON object with a ``type`` field:
    - ``match``: a matching line (fields: ``path.text``,
      ``data.lines.text``, ``data.line_number``)
    - ``begin`` / ``end`` / ``summary``: ignored

    Returns dict with ``matches`` (list of ``{file, line, text}``),
    ``shown``, ``total``, ``truncated``, and optionally ``next_offset``.
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
        if len(results) > max_results:
            break
    return _build_search_result(results, max_results)


# ---------------------------------------------------------------------------
# Parser: ripgrep --count-matches output (path:count per line)
# ---------------------------------------------------------------------------


def _parse_rg_count_output(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ripgrep ``--count-matches`` output (``path:count`` per line).

    Returns dict with ``matches`` (list of ``{file, line, text}`` where
    ``line=0`` and ``text`` is the count as string), ``shown``, ``total``,
    ``truncated``, and optionally ``next_offset``.
    """
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        idx = line.rfind(":")
        if idx == -1:
            continue
        file_path = line[:idx]
        count_str = line[idx + 1:]
        results.append(
            {
                "file": file_path,
                "line": 0,
                "text": count_str,
            }
        )
        if len(results) > max_results:
            break
    return _build_search_result(results, max_results)


# ---------------------------------------------------------------------------
# Parser: ripgrep --files-with-matches output (path per line)
# ---------------------------------------------------------------------------


def _parse_rg_files_output(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ripgrep ``--files-with-matches`` output (one path per line).

    Returns dict with ``matches`` (list of ``{file, line, text}`` where
    ``line=0`` and ``text=""``), ``shown``, ``total``, ``truncated``,
    and optionally ``next_offset``.
    """
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        results.append(
            {
                "file": line,
                "line": 0,
                "text": "",
            }
        )
        if len(results) > max_results:
            break
    return _build_search_result(results, max_results)


# ---------------------------------------------------------------------------
# Parser: grep output
# ---------------------------------------------------------------------------


def _parse_grep_output(raw: str, max_results: int) -> dict[str, Any]:
    """Parse standard ``grep -rnI`` output (``file:line:text``).

    Returns dict with ``matches`` (list of ``{file, line, text}``),
    ``shown``, ``total``, ``truncated``, and optionally ``next_offset``.
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
            if len(results) > max_results:
                break
    return _build_search_result(results, max_results)


# ---------------------------------------------------------------------------
# Parser: grep -c output (file:count per line)
# ---------------------------------------------------------------------------


def _parse_grep_count_output(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ``grep -c`` output (``file:count`` per line).

    Same format as ripgrep ``--count-matches``: ``path:count`` lines.
    Returns dict with ``matches`` (list of ``{file, line, text}`` where
    ``line=0`` and ``text`` is the count as string), ``shown``, ``total``,
    ``truncated``, and optionally ``next_offset``.
    """
    return _parse_rg_count_output(raw, max_results)


# ---------------------------------------------------------------------------
# Parser: grep -l output (file per line)
# ---------------------------------------------------------------------------


def _parse_grep_files_output(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ``grep -l`` output (one file path per line).

    Same format as ripgrep ``--files-with-matches``: one path per line.
    Returns dict with ``matches`` (list of ``{file, line, text}`` where
    ``line=0`` and ``text=""``), ``shown``, ``total``, ``truncated``,
    and optionally ``next_offset``.
    """
    return _parse_rg_files_output(raw, max_results)


# ---------------------------------------------------------------------------
# Parser: ast-grep (sg) --json output
# ---------------------------------------------------------------------------


def _parse_sg_json(raw: str, max_results: int) -> dict[str, Any]:
    """Parse ``sg run --json=stream`` output.

    ``sg run --json=stream`` outputs one JSON object per line.

    Returns dict with ``matches`` (list of ``{file, line, text}``),
    ``shown``, ``total``, ``truncated``, and optionally ``next_offset``.
    """
    raw = raw.strip()
    if not raw:
        return _build_search_result([], max_results)

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
            if len(results) > max_results:
                break
        if len(results) > max_results:
            break
    return _build_search_result(results, max_results)
