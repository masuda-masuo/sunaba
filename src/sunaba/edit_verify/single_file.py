"""Single-file lint/type_check tools (backward-compatible MCP tool handlers)."""

from __future__ import annotations

from typing import Any

from .lint_runners import (
    _run_eslint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
from .parsers import _parse_pylint_output
from .paths import ScopeWorkdir, _get_extension
from .shell import _SANDBOX_ENV, _quote_path


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
