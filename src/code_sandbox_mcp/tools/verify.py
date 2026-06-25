"""Verify tools: apply_patch, transform_file, search, lint, type_check, verify."""

from __future__ import annotations

import json

from docker.errors import NotFound

from code_sandbox_mcp.edit_verify import (
    apply_patch_to_file,
    lint_file,
    search_files,
    transform_file_in_container,
    type_check_file,
)
from code_sandbox_mcp.output_control import paginate_output, truncate_output
from code_sandbox_mcp.tools.common import _docker


def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. note::

       This function is **no longer registered as an MCP tool** (see
       issue #256).  It remains available as an internal helper for
       machine-generated diffs.  For AI-authored edits, use
       :func:`write_file_sandbox` with ``old_str`` or
       :func:`transform_file`.

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
        append / string-replace modes (recommended for AI edits).
        :func:`transform_file` — imperative edits (bulk / structural /
        computed).
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

    *code* is executed as a **complete Python module** inside the disposable
    sandbox container (never on the host).  The **only** requirement is that,
    once the module finishes executing, a top-level callable
    ``transform(text: str) -> str`` exists — you are free to define helper
    functions, classes, ``import`` modules, and any number of other top-level
    statements alongside it.  ``transform`` is called with the file's current
    text and must return the new text; the result is written back and a
    **unified diff of the change is returned** so you can verify the effect
    without a separate read-back.

    *code* is base64-encoded before transport, so quotes (including
    triple-quoted strings), backslashes, multibyte characters, and newlines
    need no escaping — pass the program as a single ``code`` string, exactly as
    you would write it in a ``.py`` file.

    Example — uppercase every TODO marker, using a helper::

        import re

        def _to_upper(m):
            return m.group(0).upper()

        def transform(text):
            return re.sub("todo", _to_upper, text, flags=re.IGNORECASE)

    .. hint::

       For a single known string replacement prefer :func:`write_file_sandbox`
       with ``old_str``.  Reach for ``transform_file`` when the edit is better
       expressed as logic than as literal text — many occurrences, a pattern,
       or a value computed from the file.  Always check the returned ``diff``;
       an over-broad pattern can change more than intended.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Absolute path to the file inside the container.
        code: Python source defining a top-level ``transform(text: str) -> str``
            (helper functions, classes, and ``import`` statements alongside it
            are fine; only the ``transform`` callable is required).
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
    test_filter: str | None = None,
    verbose: bool = False,
    pytest_args: str | None = None,
    language: str | None = None,
) -> str:
    """Run pytest with optional filter → full-suite fallback and diff summary.

    **Use this as the pre-publish test gate.**  When *test_filter* or
    *pytest_args* is provided, the filtered tests run first; if they
    pass, the full test suite runs automatically.  The gate decision is
    always based on the full suite result.

    Lint and type-checking should be done separately with
    :func:`lint_in_container` and :func:`type_check_in_container`
    during the edit loop.

    Returns a diff summary (``git diff --stat``) so the LLM can
    present changes to the user before calling :func:`publish`.

    Args:
        container_id: 12-character container ID prefix.
        path: File or directory path inside the container (e.g.
            ``"tests/"``).
        test_filter: pytest ``-k`` expression for selective test
            execution.  When set, filtered tests run first; if
            they pass, the full suite runs automatically.
        verbose: Pass ``-v`` to pytest (default ``False``).
        pytest_args: Additional raw pytest arguments (e.g.
            ``"-x --tb=short"``).  Applied to both filtered and
            full runs.
        language: Explicit language override (``"python"``, ``"js"``,
            ``"ts"``, ``"go"``).  Skips auto-detection.

    Returns:
        JSON string with:

        * ``gate_passed``: ``True`` if full test suite passed
        * ``partial_test_run``: ``True`` when only filtered tests ran
          (filtered failed, full was never executed)
        * ``detected_languages``: list of detected language keys
        * ``tests``: result dict with ``filtered`` and/or ``full`` keys
        * ``diff_summary``: ``git diff --stat`` output
        * ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    import shlex

    from code_sandbox_mcp.edit_verify import (
        _quote_path,
        _SANDBOX_ENV,
        detect_languages,
    )

    client = _docker()
    try:
        container = client.containers.get(container_id)
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

    # --- Language detection ---
    detected = detect_languages(container, path, language)

    def _run(cmd: str) -> tuple[int, str, str]:
        ec, out = container.exec_run(
            ["/bin/sh", "-c", cmd], stdout=True, stderr=True,
        )
        out_stdout, out_stderr = (
            out if isinstance(out, tuple) else (out, b"")
        )
        stdout_text = (
            out_stdout.decode("utf-8", errors="replace") if out_stdout else ""
        )
        stderr_text = (
            out_stderr.decode("utf-8", errors="replace") if out_stderr else ""
        )
        return ec, stdout_text, stderr_text

    # --- Get diff summary ---
    diff_ec, diff_out, diff_err = _run(
        "git diff HEAD --stat 2>/dev/null && echo '---UNSTAGED---' && "
        "git diff --cached --stat 2>/dev/null"
    )
    diff_summary = (diff_out + "\n" + diff_err).strip()
    if not diff_summary:
        diff_summary = "(no changes detected)"

    # --- Determine if partial test run (filter provided) ---
    has_filter = bool(test_filter or pytest_args)
    extra_args = ""
    if test_filter:
        extra_args += f" -k {shlex.quote(test_filter)}"
    if verbose:
        extra_args += " -v"
    if pytest_args:
        extra_args += f" {pytest_args}"

    result: dict = {
        "gate_passed": False,
        "partial_test_run": False,
        "detected_languages": sorted(detected.languages),
        "tests": {},
        "diff_summary": diff_summary,
    }
    if detected.reason:
        result["detection_warning"] = detected.reason

    # --- Run pytest ---
    def _run_pytest(filter_args: str) -> dict:
        _json_file = "/tmp/_pytest_report.json"
        full_cmd = (
            f"{_SANDBOX_ENV}python3 -m pytest --json-report "
            f"--json-report-file={_json_file} -q{filter_args} "
            f"{_quote_path(path)} >/dev/null 2>&1; "
            f"_ec=$?; cat {_json_file} 2>/dev/null; "
            f"rm -f {_json_file}; exit $_ec"
        )
        ec, stdout_text, stderr_text = _run(full_cmd)

        if ec == 127:
            return {"status": "not_available", "error": "python3 not found in container"}
        if ec == 5:
            return {"status": "no_tests", "error": "no tests found"}

        stdout_text_s = stdout_text if isinstance(stdout_text, str) else ""

        if not stdout_text_s.strip():
            return {"status": "no_tests", "error": "no test output produced"}

        try:
            from code_sandbox_mcp.test_report import PytestAdapter
            report = PytestAdapter.parse_json(stdout_text_s)
            d = report.to_dict()
            return d
        except Exception:
            return {"status": "error", "error": f"failed to parse pytest output (exit {ec})"}

    if has_filter:
        # Phase 1: filtered test run
        filtered_result = _run_pytest(extra_args)
        result["tests"]["filtered"] = filtered_result
        if filtered_result.get("status") != "ok":
            result["partial_test_run"] = True
            result["gate_fail_reasons"] = [
                f"filtered tests ({filtered_result.get('status', 'unknown')}): "
                f"{filtered_result.get('failed', 0)} failed"
            ]
            return json.dumps(result)
        # Phase 2: full test suite
        full_result = _run_pytest("")
        result["tests"]["full"] = full_result
    else:
        full_result = _run_pytest(extra_args)
        result["tests"]["full"] = full_result

    if full_result.get("status") == "ok":
        result["gate_passed"] = True
    elif full_result.get("status") == "not_available":
        result["gate_fail_reasons"] = ["pytest not available in container"]
    elif full_result.get("status") == "no_tests":
        result["gate_pass_reason"] = "no tests found — gate passes"
        result["gate_passed"] = True
    else:
        result["gate_fail_reasons"] = [
            f"tests: {full_result.get('failed', 0)} failure(s)"
        ]

    return json.dumps(result)
