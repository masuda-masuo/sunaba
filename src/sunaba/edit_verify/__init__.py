"""Edit/Verify subsystem: minimal edit loop primitives for sandbox containers.

Provides low-level file editing and verification tools that operate on
disposable sandbox containers (not the real repository).  These tools
form the core of the minimal edit loop:

    search_in_container -> read_file_range -> apply_patch
    -> lint/type_check -> verify_in_container

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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sunaba.journal import record_file_write

from .drivers import _EDIT_SYMBOL_DRIVER, _GIT_APPLY_TRANSFORM, _TRANSFORM_RUNNER
from .parsers import (  # noqa: F401
    _RUFF_SEVERITY_MAP,
    _TSC_TEXT_RE,
    _determine_lint_severity,
    _parse_eslint_output,
    _parse_go_vet_output,
    _parse_golangci_lint_output,
    _parse_pylint_output,
    _parse_pyright_output,
    _parse_ruff_output,
    _parse_tsc_json,
    _parse_tsc_text,
)
from .paths import ScopeWorkdir, _determine_scope, _get_extension, _is_test_file  # noqa: F401

# Import extracted submodules
from .results import (
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
    _envelope_skipped,
)
from .shell import _GO_ENV, _SANDBOX_ENV, _path_display, _quote_path


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
    # Search both the target path and the working directory root for
    # project-level markers (e.g. pyproject.toml at repo root, found when
    # path="tests/").  The working_dir root is "." when workdir is set.
    search_paths = [shlex.quote(path)]
    if path not in (".", "", working_dir):
        search_paths.append(".")
    find_cmd = "; ".join(
        f"find {p} -maxdepth 1 \\( {or_expr} \\) 2>/dev/null"
        for p in search_paths
    )

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


#: Environment variables to set before running linters/type checkers
#: inside sandbox containers.  Containers run as a non-root user with
#: a read-only ``/``, so cache directories must point to ``/tmp``.

# ---------------------------------------------------------------------------
# JS/TS tool resolution: repo node_modules/.bin wins over the baked global
# (Issue #588)
# ---------------------------------------------------------------------------
#
# Python's ``pip install -e .[dev]`` writes into the same venv the image
# already put on PATH, so the repo naturally wins.  Node has no equivalent:
# a globally baked eslint 9 hitting a repo pinned to eslint 8's config is a
# silent version mismatch, not an error -- the worst outcome for a verify
# gate (a repo could look "clean" only because the wrong linter ran).  So
# every js/ts runner resolves per-invocation instead of trusting PATH:
# ``node_modules/.bin/<tool>`` wins when it exists, the image-baked global
# is the fallback, and *which one ran* is always surfaced in the envelope's
# ``detail`` field -- never silent.


def _resolve_js_tool(container: Any, tool: str, workdir: str | None = None) -> tuple[str, str]:
    """Resolve *tool* (``eslint`` / ``tsc`` / ``jest``) to a command + source.

    Checks ``node_modules/.bin/<tool>`` relative to *workdir* (the
    container's own working directory -- normally the repo root -- when
    *workdir* is ``None``).  Returns ``(command, source)`` where *source*
    is ``"local"`` when the repo-pinned binary exists, or ``"global"`` when
    falling back to the image-baked one on ``PATH``.
    """
    ec, _ = container.exec_run(
        ["/bin/sh", "-c", f"test -x node_modules/.bin/{tool}"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    if ec == 0:
        return f"./node_modules/.bin/{tool}", "local"
    return tool, "global"


def _annotate_resolution(result: VerifyResult, source: str, cmd: str) -> VerifyResult:
    """Stamp *result*'s ``detail`` with which eslint/tsc/jest binary ran.

    Silently using a different tool version than the repo pins is the
    worst outcome for a verify gate (#588), so every eslint/tsc/jest
    envelope must say whether it ran the repository's
    ``node_modules/.bin`` binary or the image-baked global fallback.
    Test-layer results (jest) carry a JSON test report in ``detail`` that
    downstream code parses with ``json.loads`` (``tools/verify.py``); for
    those the resolution is injected as JSON fields instead of a text
    prefix so that contract survives untouched.
    """
    if result.detail:
        try:
            payload = json.loads(result.detail)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            payload["resolved_via"] = source
            payload["resolved_cmd"] = cmd
            result.detail = json.dumps(payload)
            return result
        result.detail = f"[resolved via {source}: {cmd}] {result.detail}"
    else:
        result.detail = f"resolved via {source}: {cmd}"
    return result


def _detect_js_test_runner(container: Any, workdir: str | None = None) -> str:
    """Tell jest and vitest projects apart via ``package.json`` (design §3).

    Only a jest adapter exists today (:class:`sunaba.test_report.JestAdapter`).
    Running the jest CLI against a vitest-only project would misparse
    vitest's own output as a crash, so a vitest project is reported
    honestly instead of forced through the wrong tool.

    Returns ``"vitest"`` only when ``vitest`` appears in dependencies (or
    the ``test`` script) and ``jest`` does not -- a project migrating
    between the two, or one that runs jest via a vitest-compatible shim,
    still gets the jest path.  Returns ``"jest"`` in every other case,
    including when ``package.json`` is missing or unreadable (matches the
    tool's prior unconditional-jest behavior).

    TODO(#588 follow-up): no VitestAdapter exists yet -- add one and
    dispatch to it here once vitest support is in scope.
    """
    ec, output = container.exec_run(
        ["/bin/sh", "-c", "cat package.json 2>/dev/null || true"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    raw = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    if not raw.strip():
        return "jest"
    try:
        pkg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "jest"
    if not isinstance(pkg, dict):
        return "jest"
    deps: dict[str, Any] = {}
    deps.update(pkg.get("dependencies") or {})
    deps.update(pkg.get("devDependencies") or {})
    test_script = str((pkg.get("scripts") or {}).get("test") or "")
    has_vitest = "vitest" in deps or "vitest" in test_script
    has_jest = "jest" in deps or "jest" in test_script
    if has_vitest and not has_jest:
        return "vitest"
    return "jest"


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


def edit_symbol_in_container(
    client: Any,
    container_id: str,
    file_path: str,
    symbol: str,
    new_code: str,
    line: int | None = None,
    preserve: str = "decorators+docstring",
) -> dict[str, Any]:
    """Resolve *symbol* in a Python file and replace or delete its definition.

    Runs the fixed :data:`_EDIT_SYMBOL_DRIVER` script inside the sandbox
    container -- never caller-supplied code, unlike ``transform_file`` --
    so every error message shape stays under host control.  The file is
    parsed with ``ast``, the definition of *symbol* (decorators included)
    is replaced by *new_code* (deleted when ``new_code == ""``), the
    edited file is re-parsed, and nothing is written on a SyntaxError.

    Returns a dict with ``status`` (``"ok"`` / ``"error"``).  On success:
    ``resolved`` (qualname / kind / start_line / end_line), ``changed``
    (bool), ``diff`` (str), ``new_size`` / ``new_lines`` (int).  On
    failure: ``error`` (str).
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

    params = {"file_path": file_path, "symbol": symbol, "new_code": new_code, "line": line, "preserve": preserve}
    params_b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    nonce = secrets.token_hex(8)
    mark_a = f"<<<ES_{nonce}>>>"
    mark_b = f"<<<END_ES_{nonce}>>>"

    driver = (
        _EDIT_SYMBOL_DRIVER
        .replace("__PARAMS_B64__", params_b64)
        .replace("__MARK_A__", mark_a)
        .replace("__MARK_B__", mark_b)
    )
    driver_b64 = base64.b64encode(driver.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.es_{nonce}.py"
    cmd = (
        f"echo {shlex.quote(driver_b64)} | base64 -d > {tmpf}"
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
                "python3 is not available in this container; edit_symbol "
                "requires a Python interpreter in the sandbox image"
            )
        return {"status": "error", "error": f"edit_symbol driver produced no result: {detail}"}

    try:
        result: dict[str, Any] = json.loads(stdout_text[start + len(mark_a):end])
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"could not parse driver result: {e}"}

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
        "file_size": _compute_file_size(content),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Extension helper
# ---------------------------------------------------------------------------



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

def _run_ruff_verify(
    container: Any,
    path: str | Sequence[str],
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
    findings = _parse_ruff_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _envelope_ok("ruff", findings, ec)


def _run_eslint_verify(
    container: Any, path: str | Sequence[str], workdir: str | None = None, fix: bool = False
) -> VerifyResult:
    """Run eslint on *path*.  Returns VerifyResult envelope.

    When *fix* is ``True`` eslint is invoked with ``--fix`` so it
    rewrites *path* in place; the returned findings are the problems
    that remain *after* fixing (Issue #284).

    Resolves ``node_modules/.bin/eslint`` before the image-baked global
    (Issue #588) so a repo pinned to a different eslint major never
    silently gets linted by the wrong version; the envelope's ``detail``
    always says which one ran.
    """
    fix_arg = "--fix " if fix else ""
    cmd, source = _resolve_js_tool(container, "eslint", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} {fix_arg}--format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("eslint", "eslint not installed in container"), source, cmd
        )
    if ec not in (0, 1, 2):
        # eslint exit 2 = runtime error
        return _annotate_resolution(
            _envelope_error("eslint", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_eslint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _annotate_resolution(_envelope_ok("eslint", findings, ec), source, cmd)


def _run_golangci_lint_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run golangci-lint on *path*.  Falls back to go vet."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}golangci-lint run --out-format json {_quote_path(path)}",
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
    findings = _parse_golangci_lint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("golangci-lint", findings, ec)


def _run_go_vet_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run go vet on *path*."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go vet {_quote_path(path)}",
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
    findings = _parse_go_vet_output(stdout_text + "\n" + stderr_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("go vet", findings, ec)



def _run_pyright_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
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
    """Run tsc --noEmit on *path*.  Returns VerifyResult envelope.

    Resolves ``node_modules/.bin/tsc`` before the image-baked global
    (Issue #588); the envelope's ``detail`` always says which one ran.
    Invokes the resolved binary directly instead of ``npx`` so the
    resolution is explicit and identical across eslint/tsc/jest, rather
    than relying on npx's own (differently-behaved) fallback search.
    """
    cmd, source = _resolve_js_tool(container, "tsc", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --noEmit {_quote_path(path)} 2>&1",
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
        return _annotate_resolution(
            _envelope_not_available("tsc", "typescript (tsc) not installed in container"),
            source, cmd,
        )
    if ec not in (0, 1, 2):
        return _annotate_resolution(
            _envelope_error("tsc", combined.strip() or f"exit code {ec}", ec), source, cmd
        )

    findings = _parse_tsc_text(combined, path)
    if not findings:
        findings = _parse_tsc_json(combined, path)
    for r in findings:
        r["severity"] = "error"
    return _annotate_resolution(_envelope_ok("tsc", findings, ec), source, cmd)


def _run_pytest_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run pytest --json-report on *path*.  Returns VerifyResult envelope.

    *workdir* defaults to the container's own working directory, which is
    the repo root; pass it only to run somewhere else (e.g. a subproject).
    """
    from sunaba.test_report import (
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
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pytest", "python3 not found in container")
    if ec == 2:
        stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        _, raw_tail = split_pytest_output(stdout_text)
        detail = "test collection failed"
        if raw_tail:
            detail += f"\n{raw_tail}"
        return _envelope_error("pytest", detail, ec)
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


def _run_jest_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run jest --json on *path*.  Returns VerifyResult envelope.

    Discriminates jest vs vitest via ``package.json`` first (design §3,
    Issue #588): running the jest CLI against a vitest-only project would
    misparse vitest's own output as a crash rather than reporting the
    real gap honestly.  Resolves ``node_modules/.bin/jest`` before the
    image-baked global, same as eslint/tsc; the resolution is recorded
    in the envelope's ``detail`` (as JSON fields alongside the test
    report, since ``detail`` here is machine-parsed downstream).
    """
    runner = _detect_js_test_runner(container, workdir=workdir)
    if runner == "vitest":
        return _envelope_skipped(
            "jest",
            "package.json indicates vitest (no jest dependency); sunaba's "
            "js test dispatch only runs jest today -- no VitestAdapter yet "
            "(#588 follow-up)",
        )

    cmd, source = _resolve_js_tool(container, "jest", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --json --passWithNoTests {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("jest", "jest not installed in container"), source, cmd
        )
    if ec not in (0, 1):
        return _annotate_resolution(
            _envelope_error("jest", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _annotate_resolution(
            _envelope_skipped("jest", "no test output produced"), source, cmd
        )

    try:
        from sunaba.test_report import JestAdapter

        report = JestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        result = VerifyResult(
            tool="jest",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
        return _annotate_resolution(result, source, cmd)
    except Exception:
        detail = "failed to parse jest output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _annotate_resolution(_envelope_error("jest", detail, ec), source, cmd)


def _run_npm_test_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run ``npm test`` when ``package.json`` declares a ``scripts.test``.

    Reads the repo-root ``package.json``, checks for ``scripts.test``,
    and either delegates to ``npm test`` or falls back to
    :func:`_run_jest_verify` (the previous dispatch target).

    Returns a :class:`VerifyResult` envelope following the same status
    conventions as ``_run_go_test_verify``:
        - ``status="ok"`` on exit code 0.
        - ``status="findings"`` on non-zero exit (test failure).
        - ``status="not_available"`` when the runner/script is missing.
    """
    # 1. Read repo-root package.json
    ec, output = container.exec_run(
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}cat package.json 2>/dev/null"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    # 2. Parse & check for scripts.test
    scripts_test: str | None = None
    if stdout_text.strip():
        try:
            pkg = json.loads(stdout_text)
            scripts_test = pkg.get("scripts", {}).get("test")
        except (json.JSONDecodeError, AttributeError):
            scripts_test = None

    if not scripts_test:
        # Fall back to jest (historical behaviour)
        return _run_jest_verify(container, path, workdir=workdir)

    # 3. Run npm test
    ec, output = container.exec_run(
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}npm test --silent 2>&1"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _stderr_part = output if isinstance(output, tuple) else (output, b"")
    combined = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if ec == 0:
        return _envelope_ok("npm test", [], ec)

    # 4. Non-zero: discriminate not_available vs findings
    #    Conservative matching: only known "runner missing" strings
    #    produce not_available; everything else is a test failure.
    output_tail = "\n".join(combined.strip().split("\n")[-20:]) if combined.strip() else ""

    npm_error_no_lifecycle = (
        "npm error" in combined and "ELIFECYCLE" not in combined
    )
    if (
        "command not found" in combined
        or ": not found" in combined
        or "Missing script" in combined
        or "ENOENT" in combined
        or npm_error_no_lifecycle
    ):
        return _envelope_not_available("npm test", output_tail)

    return VerifyResult(
        tool="npm test",
        status="findings",
        detail=output_tail,
        exit_code=ec,
    )


def _run_go_test_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run go test -json on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go test -json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
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
        from sunaba.test_report import GoTestAdapter

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
        "test": _run_npm_test_verify,
    },
    "ts": {
        "lint": _run_eslint_verify,
        "type": _run_tsc_verify,
        "test": _run_npm_test_verify,
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
    container: Any, path: str | Sequence[str], lang: str, workdir: str | None
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


def _run_patch_targets_verify(
    container: Any,
    working_dir: str | None = None,
) -> VerifyResult:
    """Run ``python scripts/check_patch_targets.py`` if it exists.

    Returns ``skipped`` when the script is not present (so projects
    without it are not blocked).  Findings mirror the script's stderr
    output format ``path:lineno: patch target ...``.
    """
    ec, output = container.exec_run(
        ["/bin/sh", "-c",
         f"{_SANDBOX_ENV}test -f scripts/check_patch_targets.py && echo EXISTS || echo NOT_FOUND"],
        stdout=True, stderr=True, workdir=working_dir,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    if stdout_text.strip() != "EXISTS":
        return _envelope_skipped("check-patch-targets", "scripts/check_patch_targets.py not found")

    ec, output = container.exec_run(
        ["/bin/sh", "-c",
         f"{_SANDBOX_ENV}python scripts/check_patch_targets.py 2>&1"],
        stdout=True, stderr=True, workdir=working_dir,
    )
    stdout_part, _ = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if ec == 127:
        return _envelope_not_available("check-patch-targets", "python not found")

    findings: list[dict[str, Any]] = []
    for line in stdout_text.split("\n"):
        m = re.match(r"^(.+?):(\d+): patch target.*", line)
        if m:
            findings.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "rule": "patch-target",
                "message": line,
            })
    return VerifyResult(
        tool="check-patch-targets",
        status="findings" if findings else "ok",
        findings=findings,
        exit_code=ec,
    )


def run_lint_type_gate(
    container: Any,
    scope: str,
    *,
    lint_scope: str | Sequence[str] | None = None,
    working_dir: str | None = None,
    language: str | None = None,
    gate_on_lint: bool = True,
    gate_on_type: bool = True,
    gate_on_patch_targets: bool = False,
) -> dict[str, Any]:
    """Run lint + type-check as a pre-test gate over *scope* (Issue #293).

    Detects project languages (from the working-dir root) and runs the
    project type checker over *scope*, and the project linter over
    *lint_scope* (falling back to *scope* when *lint_scope* is omitted --
    callers that mirror CI's lint-only scope, e.g. ``src/`` + ``tests/``,
    pass both separately since CI has no matching type-check step to
    widen *scope* for).  The Python linter runs with the project's ruff
    config only -- no security extend-select -- so a failing lint gate
    means CI's ``ruff check`` would also fail.

    Gate decisions:

    * **lint** -- any finding (excluding tool-state sentinels) fails the
      gate when *gate_on_lint*.  Severity is intentionally irrelevant:
      ruff exits non-zero for *any* enabled rule (``D``/``I``/``W``
      included), so the gate mirrors CI rather than the severity
      heuristic used for presentation.  (This is why the motivating
      ``D101`` -- a "warning"-severity rule -- is caught here.)
    * **type** -- any type-checker finding fails the gate when
      *gate_on_type*.
    * **patch_targets** -- when ``scripts/check_patch_targets.py`` exists,
      any unresolved ``patch(...)`` target fails the gate when
      *gate_on_patch_targets* (default ``False``, opt-in).  Skips
      silently when the script is absent (so projects without it are
      not blocked).

    Tool absence (``not_available``) or execution errors set
    ``incomplete=True`` but do **not** fail the gate -- a missing tool is
    an environment signal (e.g. the lint/type-free ``:minimal`` image),
    not a code defect.

    Returns a dict with ``gate_passed``, ``incomplete``,
    ``detected_languages``, ``lint`` / ``types`` / ``patch_targets``
    (flat finding lists), and ``gate_fail_reasons``.
    """
    # Detect from the project root so package markers (pyproject.toml, etc.)
    # are found; the linter/type-checker then run on the CI-aligned *scope*.
    detected = detect_languages(container, ".", language, working_dir=working_dir)
    effective_lint_scope = scope if lint_scope is None else lint_scope

    lint_results: list[VerifyResult] = []
    type_results: list[VerifyResult] = []
    patch_targets_result: VerifyResult | None = None
    if gate_on_patch_targets:
        patch_targets_result = _run_patch_targets_verify(container, working_dir)

    for lang in sorted(detected.languages):
        if gate_on_lint:
            lint_results.append(
                _gate_lint_runner(container, effective_lint_scope, lang, working_dir)
            )
        if gate_on_type:
            type_results.append(_gate_type_runner(container, scope, lang, working_dir))

    gate_fail_reasons: list[str] = []
    _all_gate_results = [*lint_results, *type_results]
    if patch_targets_result is not None:
        _all_gate_results.append(patch_targets_result)
    incomplete = any(
        vr.status in ("not_available", "error")
        for vr in _all_gate_results
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

    if gate_on_patch_targets and patch_targets_result is not None:
        if patch_targets_result.status == "findings":
            gate_fail_reasons.append(
                f"patch_targets ({patch_targets_result.tool}): "
                f"{len(patch_targets_result.findings)} unresolved target(s)"
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
        "patch_targets": _flatten_layer([patch_targets_result]) if patch_targets_result is not None else [],
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
