"""Verify tools: apply_patch, search_in_container, lint, type_check, verify_in_container."""

from __future__ import annotations

import json

from docker.errors import NotFound

from code_sandbox_mcp.edit_verify import (
    _determine_scope,
    _get_extension,
    apply_patch_to_file,
    lint_file,
    search_files,
    type_check_file,
)
from code_sandbox_mcp.journal import record_tool_use
from code_sandbox_mcp.tools.common import _docker
from code_sandbox_mcp.tools.vcs import resolve_git_root


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


def search_in_container(
    container_id: str,
    pattern: str,
    path: str | None = None,
    mode: str = "lexical",
    max_results: int = 50,
    glob: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
    output_mode: str = "content",
    offset: int = 0,
) -> str:
    """Search for *pattern* inside the container using ripgrep/ast-grep.

    Returns a JSON object with ``matches`` (list), ``shown``, ``total``,
    ``truncated``, and optionally ``next_offset``.

    **Lexical** mode (default) uses ripgrep (``rg``) with regex support,
    falling back to ``grep`` if ripgrep is not installed.

    **Structural** mode uses ``ast-grep`` (``sg``) for AST-aware search
    that ignores whitespace/formatting differences.

    .. rubric:: Use when

    - Searching for text patterns across files inside the container
    - Finding function definitions, imports, or specific code patterns
    - AST-aware search via *structural* mode (ignores whitespace/formatting)

    .. rubric:: Don't use when

    - **Reading file content** — use :func:`read_file_range` instead
    - **Listing files** — use :func:`list_files` instead
    - **Running shell commands** — use :func:`sandbox_exec` instead

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec grep`` for text search (structured JSON response, language-aware fallback)

    .. rubric:: Fallback

    - If ripgrep/ast-grep is not installed, falls back to POSIX ``grep`` automatically
    - For file content reading use :func:`read_file_range`
    - For directory listing use :func:`list_files`

    Args:
        container_id: 12-character container ID prefix.
        pattern: Search pattern (regex for lexical, AST pattern for structural).
        path: Directory or file path to search within (default ``None`` = auto-detect repo root).
        mode: ``"lexical"`` (ripgrep → grep) or ``"structural"`` (ast-grep).
        max_results: Maximum results to return (default 50).
        glob: Optional glob pattern to filter files (e.g. ``"*.py"``).
        ignore_case: Case-insensitive search (default False).
        context: Number of context lines before/after match (default 0).
        output_mode: ``"content"`` (default), ``"files_with_matches"``, or ``"count"``.
        offset: Line offset for pagination (default 0).

    Returns:
        JSON string with ``matches``, ``shown``, ``total``, ``truncated``,
        and optionally ``next_offset``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    # Auto-detect repo root when path is not specified (Issue #469)
    resolved_path = path
    if resolved_path is None:
        resolved_path = resolve_git_root(container)

    record_tool_use(
        container_id[:12],
        "search_in_container",
        {"pattern": pattern, "path": resolved_path, "mode": mode},
    )
    results = search_files(
        client, container_id, pattern, path=resolved_path, mode=mode,
        max_results=max_results, glob=glob, ignore_case=ignore_case,
        context=context, output_mode=output_mode, offset=offset,
    )
    return json.dumps(results)




def lint_in_container(container_id: str, file_path: str, fix: bool = False) -> str:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a JSON array of findings, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``)
    - ``message`` (str): human-readable message

    **Two-phase check**: the linter first runs on the single file; if
    no findings are reported, it also runs on the full project scope
    (e.g. ``"src/"``) to catch issues that only appear in project-wide
    checks (like I001 import ordering).

    **Autofix** (*fix=True*): the linter applies its safe autofixes
    (``ruff check --fix`` / ``eslint --fix``) to *file_path* in place
    and returns the violations that remain *after* fixing (Issue #284).
    This removes the need to drop to ``sandbox_exec ruff check --fix``
    during the edit loop.  The autofix is scoped to *file_path* only —
    the project-wide scope phase stays read-only, so a single-file fix
    never mutates unrelated files.  Inspect the returned findings (and,
    if needed, the file diff via :func:`verify_in_container`) to see
    what could not be fixed automatically.

    Supported:
    - ``.py`` → ``ruff check`` (falls back to ``pylint``; pylint has no autofix)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` → ``eslint``

    .. rubric:: Use when

    - Checking code quality during the edit loop
    - Detecting unused imports, syntax errors, and style violations
    - **Auto-fixing** import ordering / unused imports / style (pass ``fix=True``)

    .. rubric:: Don't use when

    - **Type checking** — use :func:`type_check_in_container` instead
    - **Running tests** — use :func:`verify_in_container` instead

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec ruff check`` (structured JSON response)
    - Prefer ``fix=True`` over ``sandbox_exec ruff check --fix`` for autofixes

    .. rubric:: Fallback

    - For type checking use :func:`type_check_in_container`
    - For full pre-publish gate use :func:`verify_in_container`

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        fix: When ``True``, apply the linter's safe autofixes to
            *file_path* in place before reporting (default ``False``).

    Returns:
        JSON string of lint findings (the violations remaining after
        any autofix), or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    ext = _get_extension(file_path)
    scope_workdir = _determine_scope(file_path) if ext in (".py", ".js", ".ts", ".jsx", ".tsx") else None
    record_tool_use(
        container_id[:12],
        "lint_in_container",
        {"file_path": file_path, "fix": fix},
    )
    results = lint_file(
        client, container_id, file_path, scope_workdir=scope_workdir, fix=fix
    )
    return json.dumps(results)


def type_check_in_container(container_id: str, file_path: str) -> str:
    """Run a type checker on *file_path* inside the container.

    Returns the same format as :func:`lint_in_container`.

    **Two-phase check**: the type checker first runs on the single file;
    if no findings are reported, it also runs on the full project scope
    to catch issues that only appear in project-wide checks.

    Supported:
    - ``.py`` → ``pyright``
    - ``.ts``, ``.tsx`` → ``tsc --noEmit``

    .. rubric:: Use when

    - Checking type correctness during the edit loop
    - Catching type errors before running tests

    .. rubric:: Don't use when

    - **Lint checking** — use :func:`lint_in_container` instead
    - **Running tests** — use :func:`verify_in_container` instead

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec pyright`` (structured JSON response)

    .. rubric:: Fallback

    - For lint checking use :func:`lint_in_container`
    - For full pre-publish gate use :func:`verify_in_container`

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
        return json.dumps({"status": "error", "error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    ext = _get_extension(file_path)
    scope_workdir = _determine_scope(file_path) if ext in (".py", ".ts", ".tsx") else None
    record_tool_use(
        container_id[:12],
        "type_check_in_container",
        {"file_path": file_path},
    )
    results = type_check_file(client, container_id, file_path, scope_workdir=scope_workdir)
    return json.dumps(results)


def verify_in_container(
    container_id: str,
    path: str,
    test_filter: str | None = None,
    verbose: bool = False,
    pytest_args: str | None = None,
    language: str | None = None,
    working_dir: str | None = None,
    skip_lint_gate: bool = False,
    skip_type_gate: bool = False,
    skip_patch_targets_gate: bool = False,
) -> str:
    """Run pytest with optional filter → full-suite fallback and diff summary.

    **Use this as the pre-publish quality gate.**  It runs lint and
    type-checking on the project source (mirroring CI's
    ``ruff check src/ tests/``) as a **precondition**, then the test
    suite -- a forgotten lint can no longer slip through to CI (Issue
    #293, #417).  When *test_filter* or *pytest_args* is provided, the
    filtered tests run first; if they pass, the full test suite runs
    automatically.  The gate decision is always based on the full suite
    result.

    The lint gate runs on ``src/`` + ``tests/`` when both exist (or
    ``.`` when neither does), matching CI's actual scope so a lint-only
    violation confined to ``tests/`` can no longer pass here and only
    surface in CI (Issue #417).  The type-check gate stays scoped to
    ``src/`` (or ``.``) since CI has no type-check step to mirror.  Both
    are independent of the test *path*, so they catch project-wide
    issues regardless of which tests are selected.  If lint or
    type-checking fails, the tests are **not** run and
    ``gate_passed=false`` is returned with the findings.  Missing tools
    (e.g. the lint/type-free ``:minimal`` image) set
    ``lint_type_incomplete`` rather than failing the gate.

    :func:`lint_in_container` / :func:`type_check_in_container` remain
    available as standalone single-file checks during the edit loop.

    Returns a structured diff summary (``git diff --numstat`` + ``git diff --name-status``) so the LLM can
    present changes to the user before calling :func:`publish`.

    .. note::

       This diff summary includes **test outcomes** (gate_passed,
       test counts), so the LLM can review what will be pushed before
       calling :func:`publish` (which executes in a single step).

    .. rubric:: Use when

    - **Pre-publish test gate** — run as the final step before calling :func:`publish`
    - Running the **full test suite** after making changes
    - Running **filtered tests first** (via *test_filter*), then auto-fallback to the full suite

    .. rubric:: Don't use when

    - **A single-file lint/type check during editing** — use :func:`lint_in_container` / :func:`type_check_in_container` (verify runs the project-wide gate)
    - **Single specific test file only** — use ``sandbox_exec`` + ``python -m pytest`` instead (see refactoring rules)
    - **Running non-Python tests** — use ``sandbox_exec`` with the appropriate test runner

    .. rubric:: Prefer over

    - Prefer over individual ``python -m pytest`` calls for the final pre-publish gate
    - Prefer over manual ``git diff --numstat`` — the structured diff summary is included automatically

    .. rubric:: Fallback

    - If pytest is not available in the container, use ``sandbox_exec`` with the appropriate test runner
    - For lint/type-check during editing, use :func:`lint_in_container` / :func:`type_check_in_container`

    Args:
        container_id: 12-character container ID prefix.
        path: File or directory path inside the container (e.g.
            ``"tests/"``).  When ``working_dir`` is set, this is
            resolved relative to ``working_dir``; otherwise it is an
            absolute path or relative to the container's default directory.
        test_filter: pytest ``-k`` expression for selective test
            execution.  When set, filtered tests run first; if
            they pass, the full suite runs automatically.
        verbose: Pass ``-v`` to pytest (default ``False``).
        pytest_args: Additional raw pytest arguments (e.g.
            ``"-x --tb=short"``).  Applied to both filtered and
            full runs.
        language: Explicit language override (``"python"``, ``"js"``,
            ``"ts"``, ``"go"``).  Skips auto-detection.
        working_dir: Working directory inside the container for test
            execution.  When ``None`` (default), the git repository root
            is auto-detected via :func:`resolve_git_root` (container
            metadata written by :func:`sandbox_initialize`, then
            ``/home/sandbox``, then a scan of ``/tmp/repo/*/``) --
            no need to pass this explicitly just because the repo was
            cloned somewhere other than ``/home/sandbox``.
        skip_lint_gate: Skip the lint precondition (default ``False``).
            Use during the edit loop for faster focused-test feedback
            when lint is known clean; leave ``False`` on the final
            pre-publish call so the gate is enforced.
        skip_type_gate: Skip the type-check precondition (default
            ``False``).  Same edit-loop fast-path rationale as
            *skip_lint_gate*.
        skip_patch_targets_gate: Skip the ``check_patch_targets.py``
            precondition (default ``False``).  Same edit-loop fast-path
            rationale as *skip_lint_gate*.

    Returns:
        JSON string with:

        * ``gate_passed``: ``True`` if the lint + type + patch-target gate
          passed and the full test suite passed
        * ``lint``: lint findings from the pre-test gate (flat list)
        * ``types``: type-check findings from the pre-test gate (flat list)
        * ``patch_targets``: patch-target findings from the pre-test gate
          (flat list, empty when script absent)
        * ``lint_type_incomplete`` (optional): ``True`` when a lint/type
          tool was unavailable or errored
        * ``partial_test_run``: ``True`` when only filtered tests ran
          (filtered failed, full was never executed)
        * ``detected_languages``: list of detected language keys
        * ``tests``: result dict with ``filtered`` and/or ``full`` keys
        * ``diff_summary``: structured JSON with ``unstaged`` and
          ``staged`` file-change records
        * ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    import shlex

    from code_sandbox_mcp.edit_verify import (
        _SANDBOX_ENV,
        detect_languages,
        run_lint_type_gate,
    )
    from code_sandbox_mcp.tools.common import _parse_numstat
    from code_sandbox_mcp.tools.vcs import resolve_git_root

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

    record_tool_use(
        container_id[:12],
        "verify_in_container",
        {"path": path, "test_filter": test_filter, "verbose": verbose},
    )

    # Auto-detect the cloned repo root (Issue #313-style detection, see
    # resolve_git_root) instead of silently defaulting to /home/sandbox and
    # forcing callers to pass working_dir explicitly whenever the repo was
    # cloned elsewhere (e.g. sandbox_initialize(clone_repo=...)'s /tmp/repo/*).
    working_dir = resolve_git_root(container, working_dir)

    # --- Language detection ---
    detected = detect_languages(container, path, language, working_dir=working_dir)

    def _run(cmd: str, workdir: str | None = working_dir) -> tuple[int, str, str]:
        ec, out = container.exec_run(
            ["/bin/sh", "-c", cmd], stdout=True, stderr=True,
            workdir=workdir,
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

    # --- Get diff summary (structured JSON, issue #500) ---
    unstaged_ec, unstaged_raw, _ = _run(
        "git diff HEAD --numstat 2>/dev/null"
    )
    staged_ec, staged_raw, _ = _run(
        "git diff --cached --numstat 2>/dev/null"
    )

    def _build_diff_section(raw_text: str) -> dict:
        if not raw_text.strip():
            return {
                "files": [],
                "total_files": 0,
                "total_additions": 0,
                "total_deletions": 0,
            }
        files = _parse_numstat(raw_text.split("\n"))
        return {
            "files": files,
            "total_files": len(files),
            "total_additions": sum(f.get("additions", 0) for f in files),
            "total_deletions": sum(f.get("deletions", 0) for f in files),
        }

    diff_summary = {
        "unstaged": _build_diff_section(unstaged_raw),
        "staged": _build_diff_section(staged_raw),
    }

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

    # --- Pre-test lint + type gate (Issue #293) ---
    # Lint runs on src/ + tests/ when both exist, mirroring CI's actual
    # ``ruff check src/ tests/`` (issue #417 -- a lint-only violation
    # confined to tests/ used to slip past this gate and only surface in
    # CI).  Type-check stays scoped to src/ since CI has no type-check
    # step to mirror.  Both are independent of the test ``path``.  This
    # makes verify a single quality gate so a forgotten lint can no
    # longer slip through to CI; both must pass before the test suite
    # runs.  The skip_* flags let the edit loop get faster focused-test
    # feedback when lint/type are known clean -- the gate is still
    # enforced on the final pre-publish call where the flags are left at
    # their default (False).
    if not (skip_lint_gate and skip_type_gate and skip_patch_targets_gate):
        _, dirs_out, _ = _run(
            "for d in src tests; do test -d \"$d\" && echo \"$d\"; done"
        )
        existing_dirs = dirs_out.split()
        type_scope = "src" if "src" in existing_dirs else "."
        lint_scope: str | list[str] = existing_dirs if existing_dirs else "."
        lt_gate = run_lint_type_gate(
            container,
            type_scope,
            lint_scope=lint_scope,
            working_dir=working_dir,
            language=language,
            gate_on_lint=not skip_lint_gate,
            gate_on_type=not skip_type_gate,
            gate_on_patch_targets=not skip_patch_targets_gate,
        )
        result["lint"] = lt_gate["lint"]
        result["types"] = lt_gate["types"]
        result["patch_targets"] = lt_gate.get("patch_targets", [])
        if lt_gate["incomplete"]:
            result["lint_type_incomplete"] = True
        if not lt_gate["gate_passed"]:
            result["gate_fail_reasons"] = lt_gate["gate_fail_reasons"]
            result["tests"] = {
                "status": "skipped",
                "message": "precondition gate failed; tests not run",
            }
            return json.dumps(result)

    # --- Run pytest ---
    def _run_pytest(filter_args: str) -> dict:
        from code_sandbox_mcp.test_report import (
            PytestAdapter,
            build_pytest_cmd,
            split_pytest_output,
        )
        _json_file = "/tmp/_pytest_report.json"
        _raw_file = "/tmp/_pytest_raw.txt"
        full_cmd = build_pytest_cmd(_json_file, _raw_file, filter_args, path, _SANDBOX_ENV)
        ec, stdout_text, stderr_text = _run(full_cmd)

        if ec == 127:
            return {"status": "not_available", "error": "python3 not found in container"}
        if ec == 2:
            _, raw_tail = split_pytest_output(stdout_text)
            return {"status": "collection_error", "error": "test collection failed", "raw_output": raw_tail}
        if ec == 5:
            return {"status": "no_tests", "error": "no tests found"}

        json_part, raw_tail = split_pytest_output(stdout_text)

        if not json_part:
            if ("No module named pytest" in raw_tail
                    or "No module named pytest" in stderr_text):
                return {"status": "not_available", "error": "pytest not installed",
                        "raw_output": raw_tail}
            return {"status": "no_tests", "error": "no test output produced", "raw_output": raw_tail}

        try:
            raw_report = json.loads(json_part)
            report = PytestAdapter.parse(raw_report)
            d = report.to_dict()
            # Add collection metadata for better diagnostics (Issue #378)
            summary = raw_report.get("summary", {})
            d["collected"] = summary.get("collected", summary.get("total", 0))
            d["collection_errors"] = summary.get("errors", 0)
            return d
        except Exception:
            result: dict = {"status": "error", "error": f"failed to parse pytest output (exit {ec})"}
            if raw_tail:
                result["raw_output"] = raw_tail
            return result

    if has_filter:
        # Phase 1: filtered test run
        filtered_result = _run_pytest(extra_args)
        result["tests"]["filtered"] = filtered_result
        if filtered_result.get("status") != "ok":
            result["partial_test_run"] = True
            filtered_status = filtered_result.get("status", "unknown")
            if filtered_status == "collection_error":
                raw = filtered_result.get("raw_output", "")
                msg = f"filtered tests collection error: {filtered_result.get('error', 'unknown')}"
                if raw:
                    msg += f"\n{raw}"
            elif filtered_status == "not_available":
                msg = "pytest not available in container"
            elif filtered_status == "no_tests":
                msg = f"filtered tests: no tests matched '{test_filter or pytest_args}'"
            else:
                msg = (
                    f"filtered tests ({filtered_status}): "
                    f"{filtered_result.get('failed', 0)} failed"
                )
            result["gate_fail_reasons"] = [msg]
            return json.dumps(result)
        # Phase 2: full test suite
        full_result = _run_pytest("")
        result["tests"]["full"] = full_result
    else:
        full_result = _run_pytest(extra_args)
        result["tests"]["full"] = full_result

    if full_result.get("status") == "ok":
        result["gate_passed"] = True
    elif full_result.get("status") == "collection_error":
        raw = full_result.get("raw_output", "")
        msg = f"collection error: {full_result.get('error', 'unknown')}"
        if raw:
            msg += f"\n{raw}"
        result["gate_fail_reasons"] = [msg]
    elif full_result.get("status") == "not_available":
        result["gate_fail_reasons"] = ["pytest not available in container"]
    elif full_result.get("status") == "no_tests":
        if has_filter:
            result["gate_fail_reasons"] = [
                f"no tests found (explicit filter specified): {full_result.get('error', 'unknown')}"
            ]
        else:
            result["gate_pass_reason"] = "no tests found — gate passes"
            result["gate_passed"] = True
    else:
        result["gate_fail_reasons"] = [
            f"tests: {full_result.get('failed', 0)} failure(s)"
        ]

    return json.dumps(result)
