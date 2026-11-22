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
from .shell import _SANDBOX_ENV, TreeTimeout, _exec_run, _quote_path


def _is_same_or_descendant(path: str, ancestor: str) -> bool:
    """Return True when *path* equals *ancestor* or lies beneath it."""
    norm_path = posixpath.normpath(path)
    norm_ancestor = posixpath.normpath(ancestor)
    if norm_ancestor == "/":
        return norm_path.startswith("/")
    return norm_path == norm_ancestor or norm_path.startswith(norm_ancestor + "/")


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
    except TreeTimeout:
        raise
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

    probe: _TargetProjectProbe | None = None
    if cwd_is_known:
        probe = _probe_target_project_context(
            container, absolute_file, base_workdir
        )
        project_root = probe.project_root
        if project_root is not None:
            workdir = project_root
        elif is_absolute_file:
            # A standalone absolute target belongs to its own physical directory;
            # do not inherit an unrelated project rooted at the Docker WORKDIR.
            workdir = probe.target_dir or posixpath.dirname(absolute_file)
        else:
            # Preserve base_workdir for regular relative paths inside it, but
            # avoid inheriting base_workdir when a relative path escapes it via
            # a symlink or parent traversal.
            phys_check = probe.target_dir or probe.target_ancestor
            if (
                phys_check is not None
                and probe.base_workdir is not None
                and not _is_same_or_descendant(phys_check, probe.base_workdir)
            ):
                workdir = (
                    probe.target_dir
                    or probe.target_ancestor
                    or posixpath.dirname(absolute_file)
                )
            else:
                workdir = base_workdir
    else:
        workdir = (
            base_workdir
            if scope_workdir and scope_workdir[1].startswith("/")
            else None
        )

    lexical_parent = posixpath.dirname(absolute_file)
    is_aliased = bool(
        probe is not None
        and (
            (probe.target_dir is not None and probe.target_dir != lexical_parent)
            or (probe.target_file is not None and probe.target_file != absolute_file)
        )
    )
    effective_file = absolute_file
    if is_aliased and probe is not None:
        effective_file = probe.target_file or (
            posixpath.join(probe.target_dir, posixpath.basename(absolute_file))
            if probe.target_dir is not None
            else absolute_file
        )

    if not cwd_is_known and not is_absolute_file:
        target_path = posixpath.normpath(normalized_file)
    elif is_aliased:
        target_path = (
            posixpath.relpath(effective_file, workdir)
            if workdir and _is_same_or_descendant(effective_file, workdir)
            else effective_file
        )
    elif is_absolute_file:
        target_path = (
            posixpath.relpath(absolute_file, workdir)
            if workdir and _is_same_or_descendant(absolute_file, workdir)
            else absolute_file
        )
    elif workdir != base_workdir:
        target_path = posixpath.relpath(absolute_file, workdir)
    else:
        target_path = posixpath.normpath(normalized_file)

    scope_path: str | None = None
    if scope_workdir is not None:
        if is_aliased and scope_workdir == _determine_scope(file_path):
            if workdir and _is_same_or_descendant(effective_file, workdir):
                rel_effective = posixpath.relpath(effective_file, workdir)
                rel_scope = _determine_scope(rel_effective)
                if rel_scope.workdir in (".", rel_scope.scope):
                    scope_path = rel_scope.scope
                else:
                    scope_path = posixpath.normpath(
                        posixpath.join(rel_scope.workdir, rel_scope.scope)
                    )
            else:
                scope_path = (
                    (probe.target_dir if probe is not None else None)
                    or posixpath.dirname(effective_file)
                )
        else:
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
                if workdir and _is_same_or_descendant(absolute_scope, workdir):
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


class _TargetProjectProbe(NamedTuple):
    """Physical target/workdir resolution and nearest project root."""

    project_root: str | None
    target_dir: str | None
    target_file: str | None
    target_ancestor: str | None
    base_workdir: str | None


_TARGET_DIR_SENTINEL = "__SUNABA_TARGET_DIR__"
_TARGET_FILE_SENTINEL = "__SUNABA_TARGET_FILE__"
_TARGET_ANCESTOR_SENTINEL = "__SUNABA_TARGET_ANCESTOR__"
_BASE_WORKDIR_SENTINEL = "__SUNABA_BASE_WORKDIR__"
_PROJECT_ROOT_SENTINEL = "__SUNABA_PROJECT_ROOT__"


def _probe_target_project_context(
    container: Any,
    file_path: str,
    fallback_workdir: str | None,
) -> _TargetProjectProbe:
    """Resolve physical target directory/file and nearest project marker in container."""
    # Docker uses / when Config.WorkingDir is empty.
    fallback_workdir = fallback_workdir or "/"
    quoted_file = _quote_path(posixpath.normpath(file_path))
    quoted_base = _quote_path(posixpath.normpath(fallback_workdir))
    script = (
        f"_sunaba_f={quoted_file}; "
        "_sunaba_hops=0; "
        "_sunaba_dir=''; "
        "_sunaba_walk=''; "
        "while :; do "
        "_sunaba_parent=$(dirname -- \"$_sunaba_f\"); "
        "_sunaba_base=$(basename -- \"$_sunaba_f\"); "
        "if [ -d \"$_sunaba_parent\" ]; then "
        "_sunaba_phys_parent=$(CDPATH= cd -P -- \"$_sunaba_parent\" 2>&1 && pwd -P) "
        "|| { printf '%s\\n' \"$_sunaba_phys_parent\" >&2; exit 1; }; "
        "_sunaba_f=\"${_sunaba_phys_parent%/}/$_sunaba_base\"; "
        "_sunaba_dir=\"$_sunaba_phys_parent\"; "
        "_sunaba_walk=\"$_sunaba_phys_parent\"; "
        "elif [ -e \"$_sunaba_parent\" ]; then "
        "printf 'target parent is not a directory: %s\\n' \"$_sunaba_parent\" >&2; exit 1; "
        "elif [ -L \"$_sunaba_parent\" ]; then "
        "printf 'target parent is a broken symlink: %s\\n' \"$_sunaba_parent\" >&2; exit 1; "
        "else "
        "_sunaba_dir=''; "
        "_sunaba_probe=\"$_sunaba_parent\"; "
        "while [ ! -e \"$_sunaba_probe\" ] && [ ! -L \"$_sunaba_probe\" ] && [ \"$_sunaba_probe\" != / ]; do "
        "_sunaba_probe=$(dirname -- \"$_sunaba_probe\"); "
        "done; "
        "if [ -d \"$_sunaba_probe\" ]; then "
        "_sunaba_walk=$(CDPATH= cd -P -- \"$_sunaba_probe\" 2>&1 && pwd -P) "
        "|| { printf '%s\\n' \"$_sunaba_walk\" >&2; exit 1; }; "
        "else "
        "printf 'target ancestor is not a directory: %s\\n' \"$_sunaba_probe\" >&2; exit 1; "
        "fi; "
        "break; "
        "fi; "
        "if [ -L \"$_sunaba_f\" ]; then "
        "if ! command -v readlink >/dev/null 2>&1; then "
        "printf 'readlink unavailable to resolve symlink: %s\\n' \"$_sunaba_f\" >&2; exit 1; "
        "fi; "
        "_sunaba_hops=$((_sunaba_hops + 1)); "
        "if [ \"$_sunaba_hops\" -gt 40 ]; then "
        "printf 'too many levels of symbolic links: %s\\n' \"$_sunaba_f\" >&2; exit 1; "
        "fi; "
        "_sunaba_link=$(readlink -- \"$_sunaba_f\") || exit 1; "
        "case \"$_sunaba_link\" in "
        "/*) _sunaba_f=\"$_sunaba_link\" ;; "
        "*) _sunaba_f=\"${_sunaba_phys_parent%/}/$_sunaba_link\" ;; "
        "esac; "
        "continue; "
        "fi; "
        "break; "
        "done; "
        "_sunaba_base_wd=''; "
        f"if [ -d {quoted_base} ]; then "
        f"_sunaba_base_wd=$(CDPATH= cd -P -- {quoted_base} 2>&1 && pwd -P) "
        "|| { printf '%s\\n' \"$_sunaba_base_wd\" >&2; exit 1; }; "
        "fi; "
        f"printf '{_TARGET_DIR_SENTINEL}%s\\n' \"$_sunaba_dir\"; "
        f"printf '{_TARGET_FILE_SENTINEL}%s\\n' \"$_sunaba_f\"; "
        f"printf '{_TARGET_ANCESTOR_SENTINEL}%s\\n' \"$_sunaba_walk\"; "
        f"printf '{_BASE_WORKDIR_SENTINEL}%s\\n' \"$_sunaba_base_wd\"; "
        "_sunaba_d=\"$_sunaba_walk\"; "
        "while :; do "
        "for f in pyproject.toml uv.lock setup.py setup.cfg tox.ini package.json; do "
        "if [ -f \"$_sunaba_d/$f\" ]; then "
        f"printf '{_PROJECT_ROOT_SENTINEL}%s\\n' \"$_sunaba_d\"; exit 0; fi; done; "
        "if [ -d \"$_sunaba_d/.git\" ] || [ -f \"$_sunaba_d/.git\" ]; then "
        f"printf '{_PROJECT_ROOT_SENTINEL}%s\\n' \"$_sunaba_d\"; exit 0; fi; "
        "[ \"$_sunaba_d\" = / ] && break; "
        "_sunaba_d=$(dirname -- \"$_sunaba_d\"); "
        f"done; printf '{_PROJECT_ROOT_SENTINEL}\\n'"
    )
    ec, stdout, stderr = _exec_run(
        container, ["/bin/sh", "-c", script], workdir=fallback_workdir
    )
    if ec != 0:
        raise RuntimeError(stderr.strip() or f"project ancestor probe exited {ec}")

    parsed: dict[str, str | None] = {}
    sentinels = (
        _TARGET_DIR_SENTINEL,
        _TARGET_FILE_SENTINEL,
        _TARGET_ANCESTOR_SENTINEL,
        _BASE_WORKDIR_SENTINEL,
        _PROJECT_ROOT_SENTINEL,
    )
    for line in stdout.splitlines():
        matched = False
        for sentinel in sentinels:
            if line.startswith(sentinel):
                if sentinel in parsed:
                    raise RuntimeError("project ancestor probe returned malformed output")
                value = line[len(sentinel):]
                if value and not value.startswith("/"):
                    raise RuntimeError("project ancestor probe returned malformed output")
                parsed[sentinel] = posixpath.normpath(value) if value else None
                matched = True
                break
        if not matched:
            raise RuntimeError("project ancestor probe returned malformed output")

    if _PROJECT_ROOT_SENTINEL not in parsed:
        raise RuntimeError("project ancestor probe returned malformed output")

    return _TargetProjectProbe(
        project_root=parsed[_PROJECT_ROOT_SENTINEL],
        target_dir=parsed.get(_TARGET_DIR_SENTINEL),
        target_file=parsed.get(_TARGET_FILE_SENTINEL),
        target_ancestor=parsed.get(_TARGET_ANCESTOR_SENTINEL),
        base_workdir=parsed.get(_BASE_WORKDIR_SENTINEL),
    )


def _project_root_for_absolute_path(
    container: Any,
    file_path: str,
    fallback_workdir: str | None,
) -> str | None:
    """Find the nearest project marker in the container filesystem.

    This probes real directories from the resolved physical target directory
    upward; it is separate from the lexical scope/anchor inferred by
    _determine_scope. Docker WORKDIR is used only as the probe cwd and fallback
    boundary.
    """
    return _probe_target_project_context(
        container, file_path, fallback_workdir
    ).project_root


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
    except TreeTimeout:
        raise
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
        if not findings and scope_path is not None and workdir is not None:
            try:
                has_tsconfig = _has_tsconfig(container, workdir)
            except TreeTimeout:
                raise
            except Exception as exc:
                return [{
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"tsc project config probe failed: {str(exc)[:180]}",
                }]
            if has_tsconfig:
                scope_r = _run_tsc_verify(
                    container, ".", workdir=workdir, project=True
                )
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


def _has_tsconfig(container: Any, workdir: str) -> bool:
    """Check directly for tsconfig.json; raise if the filesystem probe fails."""
    ec, _, stderr = _exec_run(
        container,
        ["/bin/sh", "-c", "test -f tsconfig.json"],
        workdir=workdir,
    )
    if ec == 0:
        return True
    if ec == 1:
        return False
    raise RuntimeError(stderr.strip() or f"tsconfig probe exited {ec}")


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
