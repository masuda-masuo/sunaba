"""Single-file lint/type_check tools (backward-compatible MCP tool handlers)."""

from __future__ import annotations

import posixpath
from typing import Any, NamedTuple

from .lint_runners import (
    _run_eslint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
from .parsers import _parse_pylint_output
from .paths import ScopeWorkdir, _determine_scope, _get_extension
from .shell import _SANDBOX_ENV, _exec_run, _quote_path


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
    try:
        context = _resolve_single_file_context(container, file_path, scope_workdir)
        workdir, lint_path, scope_path = (
            context.workdir,
            context.target_path,
            context.scope_path,
        )
    except Exception as e:
        return [{
            "file": file_path,
            "line": 0,
            "rule": "error",
            "message": f"lint path resolution failed: {str(e)[:180]}",
        }]

    if ext in (".py",):
        findings = _run_python_linter(container, lint_path, fix=fix, workdir=workdir)
        if not findings and scope_path is not None:
            # Scope phase is always read-only: a single-file fix must
            # never mutate the project-wide scope (Issue #284).
            scope_r = _run_ruff_verify(container, scope_path, workdir=workdir, fix=False)
            if scope_r.status == "error":
                return _error_findings(scope_r, file_path, "ruff")
            if scope_r.status != "not_available":
                return scope_r.findings
        return findings
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        findings = _run_js_linter(container, lint_path, fix=fix, workdir=workdir)
        if not findings and scope_path is not None:
            # Scope phase is always read-only: a single-file fix must
            # never mutate the project-wide scope (Issue #284).
            scope_r = _run_eslint_verify(container, scope_path, workdir=workdir, fix=False)
            if scope_r.status == "error":
                return _error_findings(scope_r, file_path, "eslint")
            if scope_r.status != "not_available":
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


class SingleFileContext(NamedTuple):
    """Resolved execution context shared by single-file lint and type checks."""

    workdir: str | None
    target_path: str
    scope_path: str | None


def _resolve_single_file_context(
    container: Any,
    file_path: str,
    scope_workdir: ScopeWorkdir | None,
) -> SingleFileContext:
    """Resolve a target and optional lexical scope against one execution root.

    _determine_scope supplies a lexical scope plus the anchor from which that
    scope was inferred. Docker WORKDIR establishes the base for relative
    target inputs. When Docker metadata is available, the nearest marked
    project ancestor of the resolved target becomes the execution root.
    """
    normalized_file = file_path.replace("\\", "/")
    attrs = getattr(container, "attrs", None)
    config = attrs.get("Config") or {} if isinstance(attrs, dict) else {}
    configured_workdir = config.get("WorkingDir") if isinstance(config, dict) else None
    if not isinstance(configured_workdir, str) or not configured_workdir:
        configured_workdir = None

    # A present attrs mapping with empty/missing WorkingDir means cwd '/'.
    # Missing Docker metadata keeps the historical fallback behavior.
    cwd_is_known = isinstance(attrs, dict)
    base_workdir = configured_workdir or "/"
    if (
        not cwd_is_known
        and scope_workdir
        and scope_workdir[1].startswith("/")
    ):
        base_workdir = posixpath.normpath(scope_workdir[1])

    is_absolute_file = normalized_file.startswith("/")
    absolute_file = (
        posixpath.normpath(normalized_file)
        if is_absolute_file
        else posixpath.normpath(posixpath.join(base_workdir, normalized_file))
    )

    if cwd_is_known:
        project_root = _project_root_for_absolute_path(
            container, absolute_file, base_workdir
        )
        # A standalone absolute target belongs to its own directory; do not
        # inherit an unrelated project rooted at the Docker WORKDIR.
        workdir = project_root or (
            posixpath.dirname(absolute_file) if is_absolute_file else base_workdir
        )
    else:
        workdir = (
            base_workdir
            if scope_workdir and scope_workdir[1].startswith("/")
            else None
        )

    if not cwd_is_known and not is_absolute_file:
        target_path = posixpath.normpath(normalized_file)
    elif is_absolute_file:
        target_path = (
            posixpath.relpath(absolute_file, workdir)
            if workdir
            and (
                absolute_file == workdir
                or absolute_file.startswith(workdir.rstrip("/") + "/")
            )
            else absolute_file
        )
    elif workdir != base_workdir:
        target_path = posixpath.relpath(absolute_file, workdir)
    else:
        target_path = posixpath.normpath(normalized_file)

    scope_path: str | None = None
    if scope_workdir is not None:
        scope = posixpath.normpath(scope_workdir[0].replace("\\", "/"))
        anchor = posixpath.normpath(scope_workdir[1].replace("\\", "/"))

        # _determine_scope's parent fallback repeats the input parent in both
        # tuple fields. Only here do we recognize that the scope is already
        # relative to Docker WORKDIR; callers otherwise preserve its anchor.
        input_parent = posixpath.normpath(
            posixpath.dirname(normalized_file) or "."
        )
        is_parent_fallback = (
            scope_workdir == _determine_scope(file_path)
            and scope == anchor == input_parent
        )
        if not scope.startswith("/") and not is_parent_fallback:
            scope = posixpath.join(anchor, scope)

        if workdir is None and not scope.startswith("/"):
            scope_path = posixpath.normpath(scope)
        else:
            absolute_scope = posixpath.normpath(
                scope if scope.startswith("/") else posixpath.join(base_workdir, scope)
            )
            if workdir and (
                absolute_scope == workdir
                or absolute_scope.startswith(workdir.rstrip("/") + "/")
            ):
                scope_path = posixpath.relpath(absolute_scope, workdir)
            else:
                scope_path = absolute_scope

    return SingleFileContext(workdir, target_path, scope_path)


def _resolve_lint_context(
    container: Any,
    file_path: str,
    scope_workdir: ScopeWorkdir | None,
) -> tuple[str | None, str, str | None]:
    """Compatibility wrapper for the shared single-file context resolver."""
    context = _resolve_single_file_context(container, file_path, scope_workdir)
    return context.workdir, context.target_path, context.scope_path


def _project_root_for_absolute_path(
    container: Any,
    file_path: str,
    fallback_workdir: str | None,
) -> str | None:
    """Find the nearest project marker in the container filesystem.

    This probes real directories from the resolved target parent upward; it
    is separate from the lexical scope/anchor inferred by _determine_scope.
    Docker WORKDIR is used only as the probe cwd and fallback boundary.
    """
    # Docker uses / when Config.WorkingDir is empty.
    fallback_workdir = fallback_workdir or "/"
    parent = posixpath.dirname(file_path)
    script = (
        "d=" + _quote_path(parent) + "; "
        "while :; do "
        "for f in pyproject.toml uv.lock setup.py setup.cfg tox.ini package.json; do "
        "if [ -f \"$d/$f\" ]; then "
        "printf '__SUNABA_PROJECT_ROOT__%s\\n' \"$d\"; exit 0; fi; done; "
        "if [ -d \"$d/.git\" ] || [ -f \"$d/.git\" ]; then "
        "printf '__SUNABA_PROJECT_ROOT__%s\\n' \"$d\"; exit 0; fi; "
        "[ \"$d\" = / ] && break; "
        "d=$(dirname \"$d\"); "
        "done; printf '__SUNABA_PROJECT_ROOT__\\n'"
    )
    ec, stdout, stderr = _exec_run(container, ["/bin/sh", "-c", script], workdir=fallback_workdir)
    if ec != 0:
        raise RuntimeError(stderr.strip() or f"project ancestor probe exited {ec}")
    sentinel = "__SUNABA_PROJECT_ROOT__"
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].startswith(sentinel):
        raise RuntimeError("project ancestor probe returned malformed output")
    root = lines[0][len(sentinel):]
    if root and not root.startswith("/"):
        raise RuntimeError("project ancestor probe returned malformed output")
    return posixpath.normpath(root) if root else None


def _error_findings(result: Any, file_path: str, tool: str) -> list[dict[str, Any]]:
    """Keep any diagnostics and make runner execution failures visible."""
    findings = list(result.findings or [])
    detail = (result.detail or "").strip()
    findings.append({
        "file": file_path,
        "line": 0,
        "rule": "error",
        "message": f"{tool} could not run: {detail or 'unknown error'}",
    })
    return findings


def _run_python_linter(
    container: Any, file_path: str, fix: bool = False, workdir: str | None = None
) -> list[dict[str, Any]]:
    """Try ruff, fall back to pylint. Report tool absence clearly.

    When *fix* is ``True`` ruff applies its safe autofixes to
    *file_path* in place (Issue #284).  The pylint fallback has no
    autofix capability, so *fix* is a no-op on that path.
    """
    result = _run_ruff_verify(container, file_path, workdir=workdir, fix=fix)
    if result.status == "error":
        return _error_findings(result, file_path, "ruff")
    if result.status != "not_available":
        return result.findings

    # Only a genuinely absent ruff permits the pylint fallback.
    pylint_result = _run_pylint(container, file_path, workdir=workdir)
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
    container: Any, file_path: str, fix: bool = False, workdir: str | None = None
) -> list[dict[str, Any]]:
    """Try eslint.

    When *fix* is ``True`` eslint applies ``--fix`` autofixes to
    *file_path* in place (Issue #284).
    """
    result = _run_eslint_verify(container, file_path, workdir=workdir, fix=fix)
    if result.status == "error":
        # eslint is installed but the run itself failed -- most often a
        # missing or broken eslint.config.*. Preserve the established
        # user-facing error message while retaining any parsed findings.
        return _error_findings(result, file_path, "eslint")
    if result.status != "not_available":
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
    try:
        context = _resolve_single_file_context(container, file_path, scope_workdir)
    except Exception as e:
        return [{
            "file": file_path,
            "line": 0,
            "rule": "error",
            "message": f"type_check path resolution failed: {str(e)[:180]}",
        }]

    workdir = context.workdir
    target_path = context.target_path
    scope_path = context.scope_path

    if ext == ".py":
        findings = _run_python_typecheck(
            container, target_path, workdir=workdir, original_file_path=file_path
        )
        if not findings and scope_path is not None:
            scope_r = _run_pyright_verify(container, scope_path, workdir=workdir)
            if scope_r.status == "error":
                return _error_findings(scope_r, file_path, "pyright")
            if scope_r.status != "not_available":
                return scope_r.findings
        return findings
    elif ext in (".ts", ".tsx"):
        findings = _run_ts_typecheck(
            container, target_path, workdir=workdir, original_file_path=file_path
        )
        if not findings and scope_path is not None:
            scope_r = _run_tsc_verify(container, scope_path, workdir=workdir)
            if scope_r.status == "error":
                return _error_findings(scope_r, file_path, "tsc")
            if scope_r.status != "not_available":
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


def _run_python_typecheck(
    container: Any,
    file_path: str,
    workdir: str | None = None,
    original_file_path: str | None = None,
) -> list[dict[str, Any]]:
    """Try pyright for Python type checking.

    *workdir* is passed through to :func:`_run_pyright_verify` so
    pyright can discover the repository's ``[tool.pyright]`` config
    (Issue #747).  When *workdir* is ``None`` the runner falls back
    to deriving the project root from the file path.
    """
    result_path = original_file_path or file_path
    pyright_result = _run_pyright_verify(container, file_path, workdir=workdir)
    if pyright_result.status == "error":
        return _error_findings(pyright_result, result_path, "pyright")
    if pyright_result.status != "not_available":
        return pyright_result.findings

    return [
        {
            "file": result_path,
            "line": 0,
            "rule": "no-typechecker",
            "message": (
                "No Python type checker found in container. "
                "Install pyright, or use a custom image "
                "(pass --default-image to the server)."
            ),
        }
    ]


def _run_ts_typecheck(
    container: Any,
    file_path: str,
    workdir: str | None = None,
    original_file_path: str | None = None,
) -> list[dict[str, Any]]:
    """Try tsc. Uses unified runner."""
    result_path = original_file_path or file_path
    tsc_result = _run_tsc_verify(container, file_path, workdir=workdir)
    if tsc_result.status == "error":
        return _error_findings(tsc_result, result_path, "tsc")
    if tsc_result.status != "not_available":
        return tsc_result.findings

    return [
        {
            "file": result_path,
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


def _run_pylint(
    container: Any, file_path: str, workdir: str | None = None
) -> list[dict[str, Any]] | None:
    """Run ``pylint --output-format json``. Returns None if pylint is not installed."""
    exit_code, stdout_text, _ = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pylint --output-format json {_quote_path(file_path)} 2>/dev/null || true",
        ],
        workdir=workdir,
    )
    if exit_code == 127:
        return None
    return _parse_pylint_output(stdout_text, file_path)
