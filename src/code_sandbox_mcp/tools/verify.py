"""Verify tools: apply_patch, transform_file, search, lint, type_check, verify."""

from __future__ import annotations

import json

from docker.errors import NotFound

from code_sandbox_mcp.edit_verify import (
    apply_patch_to_file,
    lint_file,
    run_verify,
    search_files,
    transform_file_in_container,
    type_check_file,
)
from code_sandbox_mcp.output_control import paginate_output, truncate_output
from code_sandbox_mcp.tools.common import _docker


def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. warning::

       **Deprecated for AI-authored edits.**  Hand-written unified diffs
       almost always fail on ``@@`` header line counts or context-line
       whitespace, and each failed retry costs a full round-trip — making
       this *more* expensive than the alternatives, not less.  For AI
       editing use :func:`write_file_sandbox` with ``old_str`` (the default
       edit path) or :func:`transform_file` (imperative).  Reserve
       ``apply_patch`` for **machine-generated** diffs (``git diff`` /
       ``diff -u``), where the diff is byte-exact.

    Reads the current file from the container, applies the unified diff,
    and writes the result back.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        diff_content: Unified diff string to apply.

    Returns:
        Success message or error description.

    See also:
        :func:`write_file_sandbox` — full overwrite / line-range /
        append / string-replace modes.
        :func:`transform_file` — recommended imperative edit path; also the
        actual implementation that ``apply_patch`` now delegates to.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    return apply_patch_to_file(client, container_id, file_path, diff_content)


def transform_file(
    container_id: str,
    file_path: str,
    code: str,
    max_lines: int = 200,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Edit a file imperatively by running Python that computes the new text.

    The **imperative** edit path: instead of providing the new bytes
    (:func:`write_file_sandbox`) or a diff (:func:`apply_patch`), you provide
    *code* that transforms the file's content.  Ideal for edits that the
    declarative tools handle poorly — bulk / repetitive / structural / computed
    changes (e.g. a regex applied to every occurrence, renaming a symbol,
    re-indenting, applying a value derived from the existing text).

    *code* must define a top-level callable ``transform(text: str) -> str``.
    It is base64-encoded and executed by a Python runner **inside the
    disposable sandbox container** (never on the host), the result is written
    back, and a **unified diff of the change is returned** so you can verify
    the effect without a separate read-back.

    Passing the program as a single ``code`` string (not a shell command) means
    multibyte characters, quotes, and newlines need no escaping.

    .. hint::

       For a single known string replacement prefer :func:`write_file_sandbox`
       with ``old_str``.  Reach for ``transform_file`` when the edit is better
       expressed as logic than as literal text — many occurrences, a pattern,
       or a value computed from the file.  Always check the returned ``diff``;
       an over-broad pattern can change more than intended.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Absolute path to the file inside the container.
        code: Python source defining ``transform(text: str) -> str``.
            Executed as a **full Python interpreter** (not a restricted DSL):
            ``__builtins__``, ``open()``, ``import``, ``subprocess``, etc.
            are all available inside the disposable sandbox container.
        max_lines: Maximum diff lines to show (summary truncation).
        offset: Line offset for paging through a large diff (0-indexed).
        limit: Maximum diff lines per page.

    Returns:
        JSON string.  On success: ``status="ok"``, ``changed`` (bool),
        ``diff`` (str, paginated) and diff metadata (``shown``,
        ``total_lines``, ``truncated``, ``next_offset``, ``has_more``).
        On failure: ``status="error"`` with ``error`` (and ``traceback`` when
        the caller's code raised).

    See also:
        :func:`write_file_sandbox` — declarative edits (the default path).
        :func:`read_file_range` — inspect file content before editing.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            {"status": "error", "error": f"container {container_id[:12]} not found"}
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") == "ok" and result.get("changed"):
        display, meta = truncate_output(
            result.get("diff", ""),
            max_lines=max_lines,
            verbose="full",
        )
        page = paginate_output(display, offset=offset, limit=limit)
        return json.dumps({
            "status": "ok",
            "changed": True,
            "diff": page.content,
            "shown": meta.shown,
            "total_lines": meta.total_lines,
            "truncated": meta.truncated,
            "next_offset": page.next_offset,
            "has_more": page.has_more,
        })

    return json.dumps(result)


def search_in_container(
    container_id: str,
    pattern: str,
    path: str = "/",
    mode: str = "lexical",
    max_results: int = 50,
) -> str:
    """Search for *pattern* inside the container using ripgrep/ast-grep.

    Returns a JSON array of matches, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``text`` (str): matching line text

    **Lexical** mode (default) uses ripgrep (``rg``) with regex support,
    falling back to ``grep`` if ripgrep is not installed.

    **Structural** mode uses ``ast-grep`` (``sg``) for AST-aware search
    that ignores whitespace/formatting differences.

    Args:
        container_id: 12-character container ID prefix.
        pattern: Search pattern (regex for lexical, AST pattern for structural).
        path: Directory or file path to search within (default ``"/"``).
        mode: ``"lexical"`` (ripgrep → grep) or ``"structural"`` (ast-grep).
        max_results: Maximum results to return (default 50).

    Returns:
        JSON string with a list of match objects, each with ``file``,
        ``line`` (int), ``text`` fields.  On container-not-found returns
        a JSON object with an ``error`` field.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps([{"error": f"Container {container_id[:12]} not found"}])
    except Exception as e:
        return json.dumps([{"error": str(e)}])

    results = search_files(
        client, container_id, pattern, path=path, mode=mode, max_results=max_results
    )
    return json.dumps(results)


def lint_in_container(container_id: str, file_path: str) -> str:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a JSON array of findings, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``)
    - ``message`` (str): human-readable message

    Supported:
    - ``.py`` → ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` → ``eslint``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of lint findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = lint_file(client, container_id, file_path)
    return json.dumps(results)


def type_check_in_container(container_id: str, file_path: str) -> str:
    """Run a type checker on *file_path* inside the container.

    Returns the same format as :func:`lint_in_container`.

    Supported:
    - ``.py`` → ``pyright``
    - ``.ts``, ``.tsx`` → ``tsc --noEmit``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of type check findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = type_check_file(client, container_id, file_path)
    return json.dumps(results)


def verify_in_container(
    container_id: str,
    path: str,
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
    language: str | None = None,
) -> str:
    """Run lint + type_check + test + scan as a bundled verification.

    **Use this instead of calling** :func:`lint_in_container` **,**
    :func:`type_check_in_container` **, and pytest separately.**
    A single call runs all four analysis layers, normalises output,
    and returns a gate decision.

    Supports multi-language verification (Python / JS / TS / Go) with
    language-aware dispatch.  Auto-detects project language from *path*
    unless overridden with *language*.

    **Layers:**

    =========== ======== ============================
    Layer       Tool    Notes
    =========== ======== ============================
    lint        ruff    Python lint (``ruff check``)
    type_check  pyright Python type checking
    test        pytest  pytest with json-report
    scan        semgrep Security scanning
    =========== ======== ============================

    **Gate logic:**

    By default the gate fails when any of the following are detected:

    * lint errors (E/F/B/RUF rule codes)
    * test failures
    * semgrep ``ERROR`` findings
    * verification incomplete (tool not available or errored)

    Type-check errors and semgrep ``WARNING`` findings are
    configurable via the ``gate_on_*`` parameters.

    Args:
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
        JSON string with:

        * ``status``: ``"ok"`` or ``"failed"``
        * ``gate_passed``: ``True`` if all gate conditions are satisfied
        * ``incomplete``: ``True`` if any layer was not available / errored
        * ``detected_languages``: list of detected language keys
        * ``lint``: list of ``{file, line, rule, severity, message}``
        * ``types``: list of ``{file, line, rule, severity, message}``
        * ``tests``: ``{status, passed, failed, duration, failures?}``
        * ``scan``: list of ``{file, line, rule, severity, message}``
        * ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": f"Container {container_id[:12]} not found",
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": str(e),
        })

    result = run_verify(
        client,
        container_id,
        path,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
        language=language,
    )
    return json.dumps(result)
