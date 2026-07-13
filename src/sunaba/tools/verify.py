"""Verify tools: apply_patch, search_in_container, lint, type_check, verify_in_container."""

from __future__ import annotations

import json

from docker.errors import NotFound

from sunaba.edit_verify import (
    _determine_scope,
    _get_extension,
    apply_patch_to_file,
    lint_file,
    search_files,
    type_check_file,
)
from sunaba.journal import record_tool_use
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.vcs import resolve_git_root
from sunaba.verify_state import record_verify_success

# ---------------------------------------------------------------------------
# Tool-absence contract (Issue #584)
# ---------------------------------------------------------------------------
#
# verify must never map "my own prerequisite is missing" onto a verdict about
# the code under test.  #584 was exactly that: the container lacked
# ``pytest-json-report``, pytest rejected ``--json-report`` with a usage error,
# no JSON was produced, and the result was reported as ``no_tests`` -- "this
# project has no tests" -- which is a lie, and one that reads like a real
# finding.  Tool absence is ``not_available``; a crashed run is ``error``; only
# a *successful* pytest run may conclude anything about the tests
# (``docs/design_multilang_support.md`` §4).

#: pytest's exit code for a usage error (bad/unknown command-line option).
#: It means *our* command did not fit this pytest -- never that tests failed.
_PYTEST_USAGE_ERROR: int = 4

#: What pytest prints when the json-report plugin is not installed.
_JSON_REPORT_ABSENT_MARKER: str = "unrecognized arguments: --json-report"


def _tool_absence_detail(raw_tail: str, stderr_text: str) -> str:
    """Explain a pytest usage error in terms the caller can act on."""
    if _JSON_REPORT_ABSENT_MARKER in raw_tail or _JSON_REPORT_ABSENT_MARKER in stderr_text:
        return (
            "verify runs pytest with --json-report, but the pytest-json-report "
            "plugin is not installed in this container. The default sandbox image "
            "bakes it in, so this container was most likely started from a custom "
            "image=, or the project's own install replaced pytest. Install it "
            "(package_install pytest-json-report) or re-initialize on the default "
            "image."
        )
    return (
        "pytest rejected verify's command line (usage error). This is a tooling "
        "problem in the container, not a test result."
    )


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
    """Search in the container with ripgrep (lexical) or ast-grep (structural).

    Args:
        container_id: Container ID prefix.
        pattern: Regex (lexical) or AST pattern (structural).
        path: Directory or file to search; default auto-detects the
            repo root.
        mode: 'lexical' (rg, grep fallback) or 'structural' (ast-grep,
            whitespace-insensitive).
        max_results: Result cap.
        glob: File filter (e.g. '*.py').
        ignore_case: Case-insensitive search.
        context: Context lines around each match.
        output_mode: 'content', 'files_with_matches', or 'count'.
        offset: Pagination offset.

    Returns:
        JSON: matches, shown, total, truncated, next_offset.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
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




# Single-file autofix is #284; the project-wide phase never mutates files.
def lint_in_container(container_id: str, file_path: str, fix: bool = False) -> str:
    """Run a linter on *file_path* inside the container.

    Linter by extension: .py -> ruff (fallback pylint),
    .js/.ts/.jsx/.tsx -> eslint.  Two-phase: the single file first
    and, when clean, the project scope read-only (catches project-wide
    issues like import ordering).  fix=True applies safe autofixes to
    *file_path* only and reports the violations that remain.

    Args:
        container_id: Container ID prefix.
        file_path: File to lint.
        fix: Apply safe autofixes in place before reporting.

    Returns:
        JSON findings array (file, line, rule, message), or an error
        message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
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
        return container_not_found_error(container_id)
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


# Pre-publish gate.  Lint scope src/+tests/ mirrors CI (#293/#417); the type
# gate stays src/-scoped (CI has no type step).  Language dispatch shares
# detection with the gate via edit_verify._DISPATCH (#493).  The diff summary
# lets the LLM review what will be pushed before publish.
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
    """Run the lint/type gates then tests -- the pre-publish quality gate.

    Lint and type-check run first as a precondition, scoped to the
    project source (src/ + tests/, mirroring CI) independent of *path*;
    if they fail, tests are NOT run and gate_passed=false.  Missing
    tools set lint_type_incomplete instead of failing the gate.  The
    test phase dispatches on detected language (pytest / jest / go
    test).  With test_filter or pytest_args the filtered tests run
    first and, when they pass, the full suite runs automatically; the
    gate decision is always the full-suite result.  The response also
    carries a structured git diff summary so changes can be reviewed
    before publish.

    Args:
        container_id: Container ID prefix.
        path: Test file or directory (e.g. 'tests/'); relative to
            working_dir when that is set.
        test_filter: pytest -k expression; filtered run first, then the
            full suite on success.
        verbose: Pass -v to pytest.
        pytest_args: Extra pytest args (e.g. '-x --tb=short'); applied
            to filtered and full runs.
        language: Force 'python'/'js'/'ts'/'go'; skips auto-detection.
        working_dir: Test working directory; default auto-detects the
            root, which is also where the container works by default.
        skip_lint_gate: Skip the lint precondition (edit-loop fast
            path; leave False on the final pre-publish run).
        skip_type_gate: Like skip_lint_gate, for the type gate.
        skip_patch_targets_gate: Like skip_lint_gate, for the
            check_patch_targets gate.

    Returns:
        JSON: gate_passed, lint, types, patch_targets,
        lint_type_incomplete, partial_test_run, detected_languages,
        tests, diff_summary, gate_fail_reasons.
    """
    import shlex

    from sunaba.edit_verify import (
        _SANDBOX_ENV,
        detect_languages,
        run_lint_type_gate,
    )
    from sunaba.tools.common import _parse_numstat
    from sunaba.tools.vcs import resolve_git_root

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id, gate_passed=False)
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

    # The repo root, which for a container created by sandbox_initialize is
    # simply its working directory (see resolve_git_root).
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

    # --- Run tests (language-aware dispatch, Issue #493) ---
    def _run_inline_pytest(filter_args: str) -> dict:
        """Run pytest inline (kept for python-specific error detail)."""
        from sunaba.test_report import (
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
        if ec == _PYTEST_USAGE_ERROR:
            # pytest rejected our command line -- verify's own prerequisite is
            # missing, which says nothing about the code under test (#584).
            _, raw_tail = split_pytest_output(stdout_text)
            return {"status": "not_available",
                    "error": _tool_absence_detail(raw_tail, stderr_text),
                    "raw_output": raw_tail}
        if ec == 5:
            return {"status": "no_tests", "error": "no tests found"}

        json_part, raw_tail = split_pytest_output(stdout_text)

        if not json_part:
            if ("No module named pytest" in raw_tail
                    or "No module named pytest" in stderr_text):
                return {"status": "not_available", "error": "pytest not installed",
                        "raw_output": raw_tail}
            if _JSON_REPORT_ABSENT_MARKER in raw_tail or _JSON_REPORT_ABSENT_MARKER in stderr_text:
                return {"status": "not_available",
                        "error": _tool_absence_detail(raw_tail, stderr_text),
                        "raw_output": raw_tail}
            if ec == 0:
                return {"status": "no_tests", "error": "no test output produced",
                        "raw_output": raw_tail}
            # pytest exited non-zero *and* produced no report: it crashed or was
            # killed.  Reporting that as "no tests" would launder a broken run
            # into a benign verdict -- the exact failure mode #584 was made of.
            return {"status": "error",
                    "error": f"pytest produced no JSON report (exit {ec})",
                    "raw_output": raw_tail}

        try:
            raw_report = json.loads(json_part)
            report = PytestAdapter.parse(raw_report)
            d = report.to_dict()
            summary = raw_report.get("summary", {})
            d["collected"] = summary.get("collected", summary.get("total", 0))
            d["collection_errors"] = summary.get("errors", 0)
            return d
        except Exception:
            result: dict = {"status": "error", "error": f"failed to parse pytest output (exit {ec})"}
            if raw_tail:
                result["raw_output"] = raw_tail
            return result

    def _run_dispatch_test(lang: str, test_path: str) -> dict:
        """Run test for a single language using DISPATCH table.

        The runner would land in the repo root anyway (it is the container's
        working directory), but an explicit *working_dir* has to win.
        """
        from sunaba.edit_verify import _DISPATCH

        runner = _DISPATCH.get(lang, {}).get("test")
        if runner is None:
            return {"status": "skipped", "error": f"no test runner for {lang}"}

        try:
            vr = runner(container, test_path, workdir=working_dir)
        except Exception as e:
            return {"status": "error", "error": str(e)}

        if vr.status == "not_available":
            return {"status": "not_available", "error": vr.detail or f"{vr.tool} not available"}
        if vr.status == "error":
            detail = vr.detail or "unknown error"
            if "test collection failed" in detail:
                raw = detail.split("\n", 1)[1] if "\n" in detail else ""
                return {"status": "collection_error", "error": "test collection failed", "raw_output": raw}
            return {"status": "error", "error": detail}
        if vr.status == "skipped":
            return {"status": "no_tests", "error": vr.detail or "skipped"}

        try:
            d = json.loads(vr.detail) if vr.detail else {}
            d["status"] = "ok" if vr.status == "ok" else "failed"
            return d
        except (json.JSONDecodeError, TypeError):
            return {"status": "ok" if vr.status == "ok" else "failed",
                    "raw_output": vr.detail}

    def _run_all_tests() -> tuple[dict, bool]:
        """Run tests for all detected languages, returning results dict."""
        results = {}
        overall_ok = True

        for lang in sorted(detected.languages):
            if lang == "python":
                results[lang] = _run_inline_pytest("")
            else:
                results[lang] = _run_dispatch_test(lang, path)

            if results[lang].get("status") not in ("ok", "no_tests", "skipped"):
                overall_ok = False

        return results, overall_ok

    if has_filter:
        if "python" in detected.languages:
            # Phase 1: filtered pytest run (python only)
            filtered_result = _run_inline_pytest(extra_args)
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

            # Phase 2: full test suite for all languages
            full_results, overall_ok = _run_all_tests()
        else:
            full_results, overall_ok = _run_all_tests()
    else:
        full_results, overall_ok = _run_all_tests()

    # --- Assign tests.full (backward-compatible: single lang -> unwrap) ---
    if len(detected.languages) == 0:
        # No languages detected at the target path.  Fall back to the
        # working directory root for project-level markers (the find
        # command in detect_languages already searches "." when path
        # differs from working_dir, but still may return empty for
        # paths outside a known project).  Gate passes silently with
        # a reason so the caller knows no tests were selected.
        result["tests"]["full"] = {"status": "no_tests", "error": "no languages detected"}
        result["gate_pass_reason"] = "no languages detected \u2014 gate passes"
        result["gate_passed"] = True
        record_verify_success(container_id)
        return json.dumps(result)
    elif len(detected.languages) == 1:
        lang = list(detected.languages)[0]
        result["tests"]["full"] = full_results[lang]
        full_result = full_results[lang]
    else:
        result["tests"]["full"] = full_results
        full_result = None

    # has_filter without Python: warn but still run full
    if has_filter and "python" not in detected.languages:
        result["filter_warning"] = (
            "test_filter / pytest_args ignored: only Python supports "
            "filtered test runs"
        )

    # --- Determine gate result ---
    if len(detected.languages) == 1:
        assert full_result is not None
        if full_result.get("status") == "ok":
            result["gate_passed"] = True
        elif full_result.get("status") == "collection_error":
            raw = full_result.get("raw_output", "")
            msg = f"collection error: {full_result.get('error', 'unknown')}"
            if raw:
                msg += f"\n{raw}"
            result["gate_fail_reasons"] = [msg]
        elif full_result.get("status") == "not_available":
            err = full_result.get("error", "unknown")
            if "pytest" in err:
                msg = "pytest not available in container"
            else:
                msg = f"{err}"
            result["gate_fail_reasons"] = [msg]
        elif full_result.get("status") == "no_tests":
            if has_filter:
                result["gate_fail_reasons"] = [
                    f"no tests found (explicit filter specified): {full_result.get('error', 'unknown')}"
                ]
            else:
                result["gate_pass_reason"] = "no tests found \u2014 gate passes"
                result["gate_passed"] = True
        elif full_result.get("status") == "error":
            # The suite never ran.  Reporting the failure count here would
            # print "0 failure(s)" as the reason the gate went red.
            result["gate_fail_reasons"] = [
                f"test execution error: {full_result.get('error', 'unknown')}"
            ]
        else:
            result["gate_fail_reasons"] = [
                f"tests: {full_result.get('failed', 0)} failure(s)"
            ]
    else:
        if overall_ok:
            result["gate_passed"] = True
        else:
            reasons = []
            for lang, lr in sorted(full_results.items()):
                s = lr.get("status")
                if s == "collection_error":
                    raw = lr.get("raw_output", "")
                    msg = f"{lang}: collection error: {lr.get('error', 'unknown')}"
                    if raw:
                        msg += f"\n{raw}"
                    reasons.append(msg)
                elif s == "not_available":
                    reasons.append(f"{lang}: tests not available ({lr.get('error', 'unknown')})")
                elif s == "error":
                    reasons.append(f"{lang}: test error ({lr.get('error', 'unknown')})")
                elif s == "failed":
                    reasons.append(f"{lang}: {lr.get('failed', 0)} failure(s)")
                elif s == "no_tests":
                    if has_filter:
                        reasons.append(f"{lang}: no tests found (explicit filter)")
                    else:
                        pass
                elif s == "skipped":
                    pass
            if reasons:
                result["gate_fail_reasons"] = reasons
            if not reasons and not overall_ok:
                result["gate_pass_reason"] = "no tests found \u2014 gate passes"
                result["gate_passed"] = True

    # Track full-gate success for state-conditioned nudges (Issue #550):
    # publish warns when called without a recorded verify success.
    if result["gate_passed"]:
        record_verify_success(container_id)
    return json.dumps(result)
