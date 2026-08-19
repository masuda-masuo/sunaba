"""Edit operations for sandbox containers.

Provides diff-based patching, imperative transforms, and
symbol-level editing for files inside sandbox containers.
"""

from __future__ import annotations

import base64
import json
import posixpath
import secrets
import shlex
from typing import Any

from sunaba.journal import record_file_write

from .drivers import (
    _EDIT_SYMBOL_DRIVER,
    _GIT_APPLY_TRANSFORM,
    _TRANSFORM_RUNNER,
    _TRANSFORM_RUNNER_MULTI,
)
from .paths import _is_test_file
from .shell import _exec_run

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


def _exec_python_runner(
    container: Any,
    runner_b64: str,
    nonce: str,
    mark_a: str,
    mark_b: str,
) -> dict[str, Any]:
    """Execute an embedded Python runner inside the sandbox container.

    Shared by the single-file and multi-file transform runners: writes the
    base64-encoded runner to a temp file, executes it with ``python3``,
    removes the temp file, and parses the JSON envelope the runner emits
    between its start/end sentinels (so stray prints from the caller's code
    cannot corrupt the result envelope).
    """
    tmpf = f"/tmp/.tf_{nonce}.py"
    cmd = (
        f"echo {shlex.quote(runner_b64)} | base64 -d > {tmpf}"
        f" && python3 {tmpf}; rc=$?"
        f"; rm -f {tmpf}"
        f"; exit $rc"
    )

    exit_code, stdout_text, stderr_text = _exec_run(
        container, ["/bin/sh", "-c", cmd],
    )

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
    return result


def transform_file_in_container(
    client: Any,
    container_id: str,
    file_path: str | None,
    code: str,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Apply an imperative ``transform(text) -> text`` to files in-container.

    The caller's *code* is executed as a complete Python module; the only
    requirement is that a top-level callable ``transform(text: str) -> str``
    exists once it finishes (helper functions, classes, and imports alongside
    it are fine).  It is base64-encoded and executed by a Python runner
    **inside the disposable sandbox container** (never on the host), the
    result is written back, and a unified diff of the change is returned so
    the effect is visible without a separate read-back.

    Either *file_path* (a single file) or *paths* (an explicit list of
    absolute paths, all edited with the **same** transform in one
    all-or-nothing call) must be given -- never both.  With *paths*, every
    file is read, every new text is computed, and writes are staged to temp
    files before committing via atomic rename, so a raising transform, a
    missing/unreadable/non-writable path, or a staging failure leaves
    every file unchanged and the error names the path that caused it.

    Returns a dict with ``status`` (``"ok"`` / ``"error"``).  On success:
    ``changed`` (bool), ``diff`` (str), ``new_size`` (int) for a single
    path, or ``files`` (list of ``{path, changed, new_size, new_lines}``)
    for multiple paths.  On failure: ``error`` (str) and, when the caller's
    code raised, ``traceback`` (str).
    """
    if (file_path is None) == (paths is None):
        return {
            "status": "error",
            "error": "exactly one of file_path or paths must be given",
        }

    if paths is not None:
        if not paths:
            return {"status": "error", "error": "paths must not be empty"}
        seen: set[str] = set()
        for p in paths:
            if not isinstance(p, str) or not p.startswith("/"):
                return {"status": "error", "error": f"paths must be absolute: {p!r}"}
            if p in seen:
                return {"status": "error", "error": f"paths contains duplicate: {p!r}"}
            seen.add(p)
            canon = posixpath.normpath(p)
            if ".." in canon.split(posixpath.sep):
                return {"status": "error", "error": f"Path traversal detected: {p!r}"}
    else:
        assert file_path is not None  # mutual exclusion checked above
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

    if paths is None:
        assert file_path is not None
        runner = (
            _TRANSFORM_RUNNER
            .replace("__FILE_PATH_REPR__", repr(file_path))
            .replace("__CODE_B64__", code_b64)
            .replace("__MARK_A__", mark_a)
            .replace("__MARK_B__", mark_b)
        )
    else:
        runner = (
            _TRANSFORM_RUNNER_MULTI
            .replace("__PATHS_REPR__", repr(paths))
            .replace("__CODE_B64__", code_b64)
            .replace("__MARK_A__", mark_a)
            .replace("__MARK_B__", mark_b)
        )
    runner_b64 = base64.b64encode(runner.encode("utf-8")).decode("ascii")
    result = _exec_python_runner(container, runner_b64, nonce, mark_a, mark_b)

    if result.get("status") != "ok":
        return result

    if paths is None:
        assert file_path is not None
        if result.get("changed"):
            record_file_write(
                container_id[:12],
                posixpath.basename(file_path),
                posixpath.dirname(file_path) or "/",
                int(result.get("new_size", 0)),
                is_test=_is_test_file(file_path),
            )
    else:
        for entry in result.get("files", []):
            if entry.get("changed"):
                record_file_write(
                    container_id[:12],
                    posixpath.basename(entry["path"]),
                    posixpath.dirname(entry["path"]) or "/",
                    int(entry.get("new_size", 0)),
                    is_test=_is_test_file(entry["path"]),
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

    exit_code, stdout_text, stderr_text = _exec_run(
        container, ["/bin/sh", "-c", cmd],
    )

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

