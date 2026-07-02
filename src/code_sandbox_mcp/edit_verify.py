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

import base64
import fnmatch
import io
import json
import posixpath
import re
import secrets
import shlex
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

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


def _envelope_ok(tool: str, findings: list[dict[str, Any]] | None = None, exit_code: int = 0) -> VerifyResult:
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
    working_dir: str | None = None,
) -> DetectionResult:
    """Detect languages from a file or directory path inside the container.

    Priority:
    1. Explicit ``language=`` parameter (skip detection).
    2. File extension map, with tsconfig.json upward search for .ts files.
    3. Directory marker files and glob patterns (e.g. ``requirements*.txt``)
       via a single ``find`` exec for efficiency.

    For polyglot projects, returns a ``scope`` dict mapping each language
    to its root directory so tools can be run per sub-tree.

    Args:
        container: Docker container object.
        path: File or directory path (relative to ``working_dir``, or absolute).
        language: Explicit language override to skip detection.
        working_dir: Working directory for exec_run. When set, ``path`` is
            resolved relative to this directory.

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
            tsconfig_dir = _find_tsconfig_upward(container, path, working_dir=working_dir)
            if tsconfig_dir is not None:
                scope_dir = tsconfig_dir
        return DetectionResult(languages={lang}, scope={lang: scope_dir})

    lang_scope: dict[str, str] = {}
    find_expr_parts = []
    for pattern, _ in _DETECTION_MARKERS:
        find_expr_parts.append(f'-name "{pattern}"')
    or_expr = " -o ".join(find_expr_parts)
    find_cmd = f"find {shlex.quote(path)} -maxdepth 1 \\( {or_expr} \\) 2>/dev/null"

    ec, output = container.exec_run(
        ["/bin/sh", "-c", find_cmd],
        stdout=True,
        stderr=True,
        workdir=working_dir,
    )
    if ec == 0:
        stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
        out = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        for line_out in out.strip().split("\n"):
            line_out = line_out.strip()
            if not line_out:
                continue
            basename = posixpath.basename(line_out)
            marker_dir = posixpath.dirname(line_out)
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


def _find_tsconfig_upward(container: Any, file_path: str, working_dir: str | None = None) -> str | None:
    """Search upward from *file_path* for a tsconfig.json.

    Returns the directory containing tsconfig.json, or None if not found.
    """
    current = posixpath.dirname(posixpath.abspath(file_path))
    while True:
        ec, output = container.exec_run(
            ["/bin/sh", "-c", f'test -f {shlex.quote(posixpath.join(current, "tsconfig.json"))} && echo found || echo notfound'],
            stdout=True,
            stderr=True,
            workdir=working_dir,
        )
        stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
        out = stdout_part.decode("utf-8", errors="replace").strip() if stdout_part else ""
        if "found" in out:
            return current
        parent = posixpath.dirname(current)
        if parent == current:
            return None
        current = parent


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


def _is_test_file(file_path: str) -> bool:
    """Check whether *file_path* follows test-file naming/directory conventions.

    Heuristic:
    - File basename starts with ``test_`` or contains ``_test`` in its stem (Python/Go).
    - File basename contains ``.test.`` or ``.spec.`` (JS/TS).
    - Path contains ``/tests/``, ``/test/``, or ``/__tests__/`` segment.
    """
    norm = posixpath.normpath(file_path)
    basename = posixpath.basename(norm)
    # Strip the extension so suffix matching works for e.g. ``utils_test.go``.
    stem = basename.rsplit(".", 1)[0]
    if stem.startswith("test_") or "_test" in stem:
        return True
    if ".test." in basename or ".spec." in basename:
        return True
    parts = norm.split(posixpath.sep)
    if "tests" in parts or "test" in parts or "__tests__" in parts:
        return True
    return False


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
    uid, gid, mode = _owner_for_write(container, file_path, parent_dir)

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


def _quote_path(path: str) -> str:
    """Shell-escape a file path for use in a command string."""
    return shlex.quote(path)


def _owner_for_write(
    container: Any, file_path: str, parent_dir: str
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

    running = _stat("/proc/self", "%u %g")
    if running and len(running) == 2:
        try:
            return int(running[0]), int(running[1]), 0o644
        except ValueError:
            pass

    return 999, 999, 0o644


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


def _needs_pcre2(pattern: str) -> bool:
    """Check if the regex pattern requires PCRE2 (look-around)."""
    return bool(re.search(r'\(\?(?:[=!]|<=|<!)', pattern))


def _search_lexical(
    container: Any,
    pattern: str,
    path: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Lexical search: ripgrep first, grep fallback."""
    quoted_pattern = shlex.quote(pattern)
    quoted_path = shlex.quote(path)

    pcre2_flag = " -P" if _needs_pcre2(pattern) else ""
    cmd = f"rg --json -n {quoted_pattern} {quoted_path} -I{pcre2_flag}"
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

    cmd = f"grep -rnI {quoted_pattern} {quoted_path}"
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

    cmd = f"sg run -p {quoted_pattern} {quoted_path} --json=stream"
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
    "mkdir -p /tmp/.ruff_cache 2>/dev/null; "
)


# ---------------------------------------------------------------------------
# Public API: called by @mcp.tool() handlers in server.py
# ---------------------------------------------------------------------------


def _normalize_diff_for_git(diff_content: str) -> str | None:
    """Reduce an arbitrary unified diff to a clean single-file patch.

    Drops all pre-hunk metadata (``diff --git`` / ``index`` / original
    ``---`` / ``+++`` lines) and re-emits deterministic ``a/target`` /
    ``b/target`` headers so ``git apply -p1`` targets a known basename
    regardless of how the caller wrote the original headers.  Everything from
    the first ``@@`` onward (all hunks) is preserved verbatim — ``git apply
    --recount`` fixes any wrong line counts.  Returns ``None`` when the diff
    contains no hunks or spans multiple files.
    """
    body: list[str] = []
    in_body = False
    for line in diff_content.split("\n"):
        if line.startswith("@@"):
            in_body = True
        if in_body:  # not elif: @@ line must also be appended to body
            # A '--- ' or '+++ ' line inside the body signals a second file
            # header (multi-file diff). apply_patch targets a single file only.
            if body and (line.startswith("--- ") or line.startswith("+++ ")):
                return None
            body.append(line)
    if not body:
        return None
    return "\n".join(["--- a/target", "+++ b/target", *body]).rstrip("\n") + "\n"


# Generated transform applied by :func:`apply_patch_to_file`.  Runs inside the
# container via :func:`transform_file_in_container`: writes the file's text and
# the (normalized) diff to a temp dir and lets ``git apply --recount`` apply it
# — tolerating off-by-one ``@@`` counts that break a strict parser.
# ``__DIFF_B64__`` is substituted on the host.
_GIT_APPLY_TRANSFORM = r'''
import base64, os, subprocess, tempfile

DIFF = base64.b64decode("__DIFF_B64__").decode("utf-8")

def transform(text):
    d = tempfile.mkdtemp()
    target = os.path.join(d, "target")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    patch = os.path.join(d, "patch.diff")
    with open(patch, "w", encoding="utf-8", newline="") as fh:
        fh.write(DIFF)
    errors = []
    for extra in ([], ["--ignore-whitespace"]):
        try:
            proc = subprocess.run(
                ["git", "apply", "--recount", "-p1", *extra, patch],
                cwd=d, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise RuntimeError("git is not available in this container")
        if proc.returncode == 0:
            with open(target, "r", encoding="utf-8") as fh:
                return fh.read()
        msg = (proc.stderr or proc.stdout).strip()
        if msg:
            errors.append(msg)
    raise RuntimeError("git apply could not apply the diff: " + " | ".join(errors))
'''


def apply_patch_to_file(
    client: Any,
    container_id: str,
    file_path: str,
    diff_content: str,
) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. note::

       ``apply_patch`` is **no longer registered as an MCP tool** (see
       issue #256).  The function remains as an internal helper that
       delegates to :func:`transform_file_in_container`, which runs
       ``git apply --recount`` **inside the container** — more robust
       for machine-generated diffs than the previous strict host-side
       parser, and consolidating diff application onto the imperative
       edit path.
    """
    if not diff_content.strip():
        return f"Patch applied (no changes) to {file_path} in container {container_id[:12]}"

    normalized = _normalize_diff_for_git(diff_content)
    if normalized is None:
        return (
            "Error: failed to apply diff: no hunks (@@) found, or diff spans "
            "multiple files (apply_patch targets a single file)"
        )

    code = _GIT_APPLY_TRANSFORM.replace(
        "__DIFF_B64__",
        base64.b64encode(normalized.encode("utf-8")).decode("ascii"),
    )
    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") != "ok":
        return f"Error: failed to apply diff: {result.get('error')}"
    if not result.get("changed"):
        return f"Patch applied (no changes) to {file_path} in container {container_id[:12]}"
    return f"Patch applied successfully to {file_path} in container {container_id[:12]}"


# ---------------------------------------------------------------------------
# Imperative (programmatic) edit: transform_file
# ---------------------------------------------------------------------------

# In-container runner.  Reads the target file, runs the caller's
# ``transform(text) -> text`` against it, writes the result back, and emits a
# unified diff wrapped in per-call sentinels so stray prints from the caller's
# code cannot corrupt the result envelope.  ``__FILE_PATH_REPR__`` /
# ``__CODE_B64__`` / ``__MARK_A__`` / ``__MARK_B__`` are substituted on the host.
_TRANSFORM_RUNNER = r'''
import sys, json, base64, difflib, traceback

FILE_PATH = __FILE_PATH_REPR__
USER_CODE_B64 = "__CODE_B64__"
MARK_A = "__MARK_A__"
MARK_B = "__MARK_B__"

def emit(obj):
    sys.stdout.write(MARK_A + json.dumps(obj) + MARK_B)
    sys.stdout.flush()
    sys.exit(0)

try:
    with open(FILE_PATH, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()
except FileNotFoundError:
    emit({"status": "error", "error": "file not found: " + FILE_PATH})
except Exception as e:
    emit({"status": "error", "error": "read failed: " + repr(e)})

try:
    user_code = base64.b64decode(USER_CODE_B64).decode("utf-8")
except Exception as e:
    emit({"status": "error", "error": "could not decode code: " + repr(e)})

ns = {}
try:
    exec(user_code, ns)
except Exception as e:
    emit({"status": "error",
          "error": "code failed at definition time: " + type(e).__name__ + ": " + str(e),
          "traceback": traceback.format_exc()})

transform = ns.get("transform")
if not callable(transform):
    emit({"status": "error",
          "error": "code must define a callable `transform(text: str) -> str`"})

try:
    new = transform(original)
except Exception as e:
    emit({"status": "error",
          "error": "transform() raised " + type(e).__name__ + ": " + str(e),
          "traceback": traceback.format_exc()})

if not isinstance(new, str):
    emit({"status": "error",
          "error": "transform() must return str, got " + type(new).__name__})

if new == original:
    emit({"status": "ok", "changed": False, "diff": "", "new_size": len(original)})

try:
    with open(FILE_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
except Exception as e:
    emit({"status": "error", "error": "write failed: " + repr(e)})

diff = "\n".join(difflib.unified_diff(
    original.splitlines(), new.splitlines(),
    fromfile=FILE_PATH, tofile=FILE_PATH, lineterm=""))
emit({"status": "ok", "changed": True, "diff": diff, "new_size": len(new)})
'''


def transform_file_in_container(
    client: Any,
    container_id: str,
    file_path: str,
    code: str,
) -> dict[str, Any]:
    """Apply an imperative ``transform(text) -> text`` to a file in-container.

    The caller's *code* is executed as a complete Python module; the only
    requirement is that a top-level callable ``transform(text: str) -> str``
    exists once it finishes (helper functions, classes, and imports alongside
    it are fine).  It is base64-encoded and executed by a Python runner
    **inside the disposable sandbox container** (never on the host), the result
    is written back, and a unified diff of the change is returned so the effect
    is visible without a separate read-back.

    Returns a dict with ``status`` (``"ok"`` / ``"error"``).  On success:
    ``changed`` (bool), ``diff`` (str), ``new_size`` (int).  On failure:
    ``error`` (str) and, when the caller's code raised, ``traceback`` (str).
    """
    if not file_path.startswith("/"):
        return {"status": "error", "error": f"file_path must be absolute: {file_path!r}"}
    canon = posixpath.normpath(file_path)
    if ".." in canon.split(posixpath.sep):
        return {"status": "error", "error": f"Path traversal detected: {file_path!r}"}

    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return {"status": "error", "error": f"Container {container_id[:12]} not found: {e}"}

    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    nonce = secrets.token_hex(8)
    mark_a = f"<<<TF_{nonce}>>>"
    mark_b = f"<<<END_TF_{nonce}>>>"

    runner = (
        _TRANSFORM_RUNNER
        .replace("__FILE_PATH_REPR__", repr(file_path))
        .replace("__CODE_B64__", code_b64)
        .replace("__MARK_A__", mark_a)
        .replace("__MARK_B__", mark_b)
    )
    runner_b64 = base64.b64encode(runner.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.tf_{nonce}.py"
    cmd = (
        f"echo {shlex.quote(runner_b64)} | base64 -d > {tmpf}"
        f" && python3 {tmpf}; rc=$?"
        f"; rm -f {tmpf}"
        f"; exit $rc"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    start = stdout_text.find(mark_a)
    end = stdout_text.find(mark_b)
    if start == -1 or end == -1:
        detail = stderr_text.strip() or stdout_text.strip() or "no output"
        if "python3" in detail and ("not found" in detail or "No such file" in detail):
            detail = (
                "python3 is not available in this container; transform_file "
                "requires a Python interpreter in the sandbox image"
            )
        return {"status": "error", "error": f"transform runner produced no result: {detail}"}

    try:
        result: dict[str, Any] = json.loads(stdout_text[start + len(mark_a):end])
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"could not parse runner result: {e}"}

    if result.get("status") == "ok" and result.get("changed"):
        record_file_write(
            container_id[:12],
            posixpath.basename(file_path),
            posixpath.dirname(file_path) or "/",
            int(result.get("new_size", 0)),
            is_test=_is_test_file(file_path),
        )
    return result


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


_RUFF_SECURITY_SELECT = ",".join([
    # shell injection
    "S102", "S602", "S603", "S604", "S605", "S606", "S607",
    # eval / exec
    "S307",
    # deserialization
    "S301", "S302", "S506",
    # TLS / SSL
    "S501", "S502", "S503", "S504",
    # weak hash
    "S324",
    # XML (XXE)
    "S313", "S314", "S315", "S316", "S317", "S318", "S319",
    # network safety
    "S113", "S507",
    # template injection
    "S701",
])

_RUFF_SECURITY_IGNORE = ",".join([
    # S101: assert is idiomatic in pytest and common for invariant guards in
    # application code (e.g. `assert x is not None`). Excluding it avoids
    # flooding test suites; the trade-off is that non-test assert-as-guard
    # patterns are not flagged. Acceptable because LLMs can reason about
    # assert usage from context without a dedicated lint signal.
    "S101",
    "S105", "S106", "S107",  # hardcoded-password heuristics — high false-positive rate
    "S311",          # random — usually non-security
    "S110", "S112",  # try-except-pass / try-except-continue — style, not security
])
class ScopeWorkdir(NamedTuple):
    """``(scope, workdir)`` tuple with named field access.

    Return type for :func:`_determine_scope`.
    """
    scope: str
    workdir: str


def _determine_scope(file_path: str) -> ScopeWorkdir:
    """Determine the project scope and working directory for lint/type-check.

    Returns a ``(scope, workdir)`` tuple:

    * *scope* — path to pass to the tool (e.g. ``"src"``, ``"."``).
    * *workdir* — project-root directory that should be the CWD when
      running scope checks (e.g. ``"/app"``, ``"."``).

    Both values are derived from *file_path* so callers no longer need
    to call :func:`_resolve_workdir` separately.

    Examples
    --------
    >>> _determine_scope("/app/src/foo.py")
    ('src', '/app')
    >>> _determine_scope("src/foo.py")
    ('src', '.')
    >>> _determine_scope("/home/foo.py")
    ('/home', '/home')
    >>> _determine_scope("foo.py")
    ('.', '.')
    """
    normalized = file_path.replace("\\", "/")
    idx = normalized.find("/src/")
    if idx != -1:
        return ScopeWorkdir("src", normalized[:idx] or ".")
    if normalized.startswith("src/"):
        return ScopeWorkdir("src", ".")
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    scope = parent or "."
    return ScopeWorkdir(scope, scope)


def _run_ruff_verify(
    container: Any,
    path: str,
    workdir: str | None = None,
    extra_select: bool = True,
    fix: bool = False,
) -> VerifyResult:
    """Run ruff on *path*.  Returns VerifyResult envelope.

    When *extra_select* is ``True`` (default) the curated security
    rule-set is layered on top of the project's own ruff config for
    awareness during editing.  Pass ``extra_select=False`` to run ruff
    with the project config **only** -- this mirrors CI's plain
    ``ruff check`` exactly and is what the pre-test gate uses, so the
    gate never diverges from CI on rules the project hasn't opted into.

    When *fix* is ``True`` ruff is invoked with ``--fix`` so it applies
    its safe autofixes (import sorting, unused-import removal, etc.) to
    *path* in place; the returned findings are the violations that
    remain *after* fixing (Issue #284).
    """
    # _quote_path uses shlex.quote (single-quote wrapping), so paths with
    # spaces or special characters are safe. SELECT/IGNORE are comma-separated
    # rule codes with no whitespace, so no quoting is needed for those.
    security_args = (
        f"--extend-select {_RUFF_SECURITY_SELECT} "
        f"--extend-ignore {_RUFF_SECURITY_IGNORE} "
        if extra_select
        else ""
    )
    fix_arg = "--fix " if fix else ""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json "
            f"{fix_arg}"
            f"{security_args}"
            f"{_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
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


def _run_eslint_verify(
    container: Any, path: str, workdir: str | None = None, fix: bool = False
) -> VerifyResult:
    """Run eslint on *path*.  Returns VerifyResult envelope.

    When *fix* is ``True`` eslint is invoked with ``--fix`` so it
    rewrites *path* in place; the returned findings are the problems
    that remain *after* fixing (Issue #284).
    """
    fix_arg = "--fix " if fix else ""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}eslint {fix_arg}--format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
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


def _run_pyright_verify(container: Any, path: str, workdir: str | None = None) -> VerifyResult:
    """Run pyright on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pyright", "pyright not installed in container")

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_pyright_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"

    if ec not in (0, 1) and not findings:
        return _envelope_error("pyright", stderr_text.strip() or f"exit code {ec}", ec)

    return _envelope_ok("pyright", findings, ec)


def _run_tsc_verify(container: Any, path: str, workdir: str | None = None) -> VerifyResult:
    """Run tsc --noEmit on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}npx tsc --noEmit {_quote_path(path)} 2>&1",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
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
    from code_sandbox_mcp.test_report import (
        PytestAdapter,
        build_pytest_cmd,
        split_pytest_output,
    )
    _json_file = "/tmp/_pytest_report.json"
    _raw_file = "/tmp/_pytest_raw.txt"
    cmd = build_pytest_cmd(_json_file, _raw_file, "", _quote_path(path), _SANDBOX_ENV)
    ec, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pytest", "python3 not found in container")
    if ec == 5:
        return _envelope_skipped("pytest", "no tests found")
    if ec not in (0, 1):
        return _envelope_error("pytest", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    json_part, raw_tail = split_pytest_output(stdout_text)

    if not json_part:
        detail = "no test output produced"
        if raw_tail:
            detail += f"\n--- raw output ---\n{raw_tail}"
        return _envelope_skipped("pytest", detail)

    try:
        report = PytestAdapter.parse_json(json_part)
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
        detail = "failed to parse pytest output"
        if raw_tail:
            detail += f"\n--- raw output ---\n{raw_tail}"
        return _envelope_error("pytest", detail, ec)


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
        detail = "failed to parse jest output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _envelope_error("jest", detail, ec)


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
        detail = "failed to parse go test output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _envelope_error("go test", detail, ec)


# ---------------------------------------------------------------------------
# Unified dispatch table
# ---------------------------------------------------------------------------
# Maps language -> layer -> runner function.
# Python type layer uses pyright.
# Go lint tries golangci-lint first, falls back to go vet.
# JS has no type layer (skipped).  Go type is covered by go vet/build.


_DISPATCH: dict[str, dict[str, Any]] = {
    "python": {
        "lint": _run_ruff_verify,
        "type": _run_pyright_verify,  # primary
        "test": _run_pytest_verify,
    },
    "js": {
        "lint": _run_eslint_verify,
        "type": None,  # skipped
        "test": _run_jest_verify,
    },
    "ts": {
        "lint": _run_eslint_verify,
        "type": _run_tsc_verify,
        "test": _run_jest_verify,
    },
    "go": {
        "lint": _run_golangci_lint_verify,
        "type": None,  # skipped: build/vet covers typing
        "test": _run_go_test_verify,
    },
    "unknown": {
        "lint": None,
        "type": None,
        "test": None,
    },
}


# ---------------------------------------------------------------------------
# lint_file / type_check_file (single-file, backward-compatible)
# ---------------------------------------------------------------------------


def lint_file(
    client: Any,
    container_id: str,
    file_path: str,
    scope_workdir: ScopeWorkdir | None = None,
    fix: bool = False,
) -> list[dict[str, Any]]:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a list of dicts, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``, ``"unused-import"``)
    - ``message`` (str): human-readable message

    When *scope_workdir* (a :class:`ScopeWorkdir` from
    :func:`_determine_scope`) is provided and the single-file check
    passes, the linter is also run on the full scope to catch issues
    that only appear in project-wide checks (like I001 import ordering).

    When *fix* is ``True`` the linter applies its safe autofixes
    (``ruff check --fix`` / ``eslint --fix``) to *file_path* in place,
    and the returned findings are the violations that remain *after*
    fixing (Issue #284).  The autofix is scoped to *file_path* only;
    the project-wide ``scope_workdir`` phase always stays read-only so
    a single-file fix never mutates unrelated files.

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
        findings = _run_python_linter(container, file_path, fix=fix)
        if not findings and scope_workdir:
            scope_path, workdir = scope_workdir
            # Scope phase is always read-only: a single-file fix must
            # never mutate the project-wide scope (Issue #284).
            scope_r = _run_ruff_verify(container, scope_path, workdir=workdir, fix=False)
            if scope_r.status not in ("not_available", "error"):
                return scope_r.findings
        return findings
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        findings = _run_js_linter(container, file_path, fix=fix)
        if not findings and scope_workdir:
            scope_path, workdir = scope_workdir
            # Scope phase is always read-only: a single-file fix must
            # never mutate the project-wide scope (Issue #284).
            scope_r = _run_eslint_verify(container, scope_path, workdir=workdir, fix=False)
            if scope_r.status not in ("not_available", "error"):
                return scope_r.findings
        return findings
    else:
        return [
            {
                "file": file_path,
                "line": 0,
                "rule": "no-linter",
                "message": f"No linter configured for {ext} files",
            }
        ]


def _run_python_linter(
    container: Any, file_path: str, fix: bool = False
) -> list[dict[str, Any]]:
    """Try ruff, fall back to pylint. Report tool absence clearly.

    When *fix* is ``True`` ruff applies its safe autofixes to
    *file_path* in place (Issue #284).  The pylint fallback has no
    autofix capability, so *fix* is a no-op on that path.
    """
    result = _run_ruff_verify(container, file_path, fix=fix)
    if result.status not in ("not_available", "error"):
        return result.findings

    # ruff not available, try pylint (no autofix support)
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


def _run_js_linter(
    container: Any, file_path: str, fix: bool = False
) -> list[dict[str, Any]]:
    """Try eslint.

    When *fix* is ``True`` eslint applies ``--fix`` autofixes to
    *file_path* in place (Issue #284).
    """
    result = _run_eslint_verify(container, file_path, fix=fix)
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
    scope_workdir: ScopeWorkdir | None = None,
) -> list[dict[str, Any]]:
    """Run a type checker on *file_path* inside the container.

    Returns the same structure as :func:`lint_file`.
    If no type checker is installed, returns ``rule: "no-typechecker"``.

    When *scope_workdir* (a :class:`ScopeWorkdir` from
    :func:`_determine_scope`) is provided and the single-file check
    passes, the type checker is also run on the full scope to catch
    issues that only appear in project-wide checks.

    Supported:
    - ``.py`` files -> ``pyright``
    - ``.ts``, ``.tsx`` files -> ``tsc --noEmit``
    """
    try:
        container = client.containers.get(container_id)
    except Exception as e:
        return [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]

    ext = _get_extension(file_path)

    if ext in (".py",):
        findings = _run_python_typecheck(container, file_path)
        if not findings and scope_workdir:
            scope_path, workdir = scope_workdir
            scope_r = _run_pyright_verify(container, scope_path, workdir=workdir)
            if scope_r.status not in ("not_available", "error"):
                return scope_r.findings
        return findings
    elif ext in (".ts", ".tsx"):
        findings = _run_ts_typecheck(container, file_path)
        if not findings and scope_workdir:
            scope_path, workdir = scope_workdir
            scope_r = _run_tsc_verify(container, scope_path, workdir=workdir)
            if scope_r.status not in ("not_available", "error"):
                return scope_r.findings
        return findings
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
    """Try pyright for Python type checking."""
    pyright_result = _run_pyright_verify(container, file_path)
    if pyright_result.status not in ("not_available", "error"):
        return pyright_result.findings

    return [
        {
            "file": file_path,
            "line": 0,
            "rule": "no-typechecker",
            "message": (
                "No Python type checker found in container. "
                "Install pyright, or use a custom image "
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

    return result


# ---------------------------------------------------------------------------
# verify: bundled lint + type_check + test + scan  (Issue #54)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pre-test lint + type gate (Issue #293)
# ---------------------------------------------------------------------------

#: "rule" values that denote tool-state, not a real code finding, and so
#: must never fail the gate.  ``no-linter`` / ``no-typechecker`` are the
#: tool-absence sentinels; ``error`` is the rule emitted by
#: :func:`lint_file` / :func:`_run_python_linter` when the container call
#: itself fails (e.g. container not found) -- defensive, since the gate
#: runs the ``*_verify`` runners directly (which signal that via
#: ``status="error"`` instead), but kept for alignment with that convention.
_GATE_SENTINEL_RULES = ("no-linter", "no-typechecker", "error")


def _gate_lint_runner(
    container: Any, path: str, lang: str, workdir: str | None
) -> VerifyResult:
    """Lint runner for the gate.  Python ruff runs WITHOUT the security
    extend-select so the gate matches CI's plain ``ruff check`` exactly."""
    if lang == "python":
        return _run_ruff_verify(container, path, workdir=workdir, extra_select=False)
    if lang in ("js", "ts"):
        return _run_eslint_verify(container, path, workdir=workdir)
    if lang == "go":
        return _run_golangci_lint_verify(container, path)
    return _envelope_skipped(f"{lang}-lint", f"language '{lang}' has no lint layer")


def _gate_type_runner(
    container: Any, path: str, lang: str, workdir: str | None
) -> VerifyResult:
    """Type-check runner for the gate."""
    if lang == "python":
        return _run_pyright_verify(container, path, workdir=workdir)
    if lang == "ts":
        return _run_tsc_verify(container, path, workdir=workdir)
    return _envelope_skipped(f"{lang}-type", f"language '{lang}' has no type layer")


def run_lint_type_gate(
    container: Any,
    scope: str,
    *,
    working_dir: str | None = None,
    language: str | None = None,
    gate_on_lint: bool = True,
    gate_on_type: bool = True,
) -> dict[str, Any]:
    """Run lint + type-check as a pre-test gate over *scope* (Issue #293).

    Detects project languages (from the working-dir root) and runs the
    project linter and type checker over *scope*.  The Python linter runs
    with the project's ruff config only -- no security extend-select --
    so a failing lint gate means CI's ``ruff check`` would also fail.

    Gate decisions:

    * **lint** -- any finding (excluding tool-state sentinels) fails the
      gate when *gate_on_lint*.  Severity is intentionally irrelevant:
      ruff exits non-zero for *any* enabled rule (``D``/``I``/``W``
      included), so the gate mirrors CI rather than the severity
      heuristic used for presentation.  (This is why the motivating
      ``D101`` -- a "warning"-severity rule -- is caught here.)
    * **type** -- any type-checker finding fails the gate when
      *gate_on_type*.

    Tool absence (``not_available``) or execution errors set
    ``incomplete=True`` but do **not** fail the gate -- a missing tool is
    an environment signal (e.g. the lint/type-free ``:minimal`` image),
    not a code defect.

    Returns a dict with ``gate_passed``, ``incomplete``,
    ``detected_languages``, ``lint`` / ``types`` (flat finding lists),
    and ``gate_fail_reasons``.
    """
    # Detect from the project root so package markers (pyproject.toml, etc.)
    # are found; the linter/type-checker then run on the CI-aligned *scope*.
    detected = detect_languages(container, ".", language, working_dir=working_dir)

    lint_results: list[VerifyResult] = []
    type_results: list[VerifyResult] = []
    for lang in sorted(detected.languages):
        if gate_on_lint:
            lint_results.append(_gate_lint_runner(container, scope, lang, working_dir))
        if gate_on_type:
            type_results.append(_gate_type_runner(container, scope, lang, working_dir))

    gate_fail_reasons: list[str] = []
    incomplete = any(
        vr.status in ("not_available", "error")
        for vr in (*lint_results, *type_results)
    )

    if gate_on_lint:
        for vr in lint_results:
            if vr.status == "findings":
                violations = [
                    r for r in vr.findings
                    if r.get("rule") not in _GATE_SENTINEL_RULES
                ]
                if violations:
                    gate_fail_reasons.append(
                        f"lint ({vr.tool}): {len(violations)} violation(s)"
                    )

    if gate_on_type:
        for vr in type_results:
            if vr.status == "findings":
                type_errors = [
                    r for r in vr.findings
                    if r.get("rule") not in _GATE_SENTINEL_RULES
                ]
                if type_errors:
                    gate_fail_reasons.append(
                        f"type_check ({vr.tool}): {len(type_errors)} error(s)"
                    )

    return {
        "gate_passed": len(gate_fail_reasons) == 0,
        "incomplete": incomplete,
        "detected_languages": sorted(detected.languages),
        "lint": _flatten_layer(lint_results),
        "types": _flatten_layer(type_results),
        "gate_fail_reasons": gate_fail_reasons,
    }


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
