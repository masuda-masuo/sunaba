"""Verify tools: apply_patch, search_in_container, lint, type_check, verify_in_container."""

from __future__ import annotations

import json

from docker.errors import NotFound

from sunaba.edit_verify import (
    _determine_scope,
    _get_extension,
    apply_patch_to_file,
    lint_file,
    type_check_file,
)
from sunaba.journal import record_tool_use
from sunaba.search import is_path_denied, search_files
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
# no report was produced, and the result was reported as ``no_tests`` -- "this
# project has no tests" -- which is a lie, and one that reads like a real
# finding.  Tool absence is ``not_available``; a crashed run is ``error``; only
# a *successful* pytest run may conclude anything about the tests
# (``docs/design_multilang_support.md`` §4).
#
# Since #785 the python path uses pytest's built-in ``--junit-xml``, so the
# plugin hole is closed at the root: no image needs to ship a plugin (or even
# pytest-xdist) for verify to work.  ``-n auto`` is passed only when
# pytest-xdist is importable in the *target* environment -- probed inside the
# pytest command itself, with a serial fallback otherwise -- so a usage error
# can only be a genuinely bad command line or a pytest too old to know
# ``--junit-xml``.

#: pytest's exit code for a usage error (bad/unknown command-line option).
#: It means *our* command did not fit this pytest -- never that tests failed.
_PYTEST_USAGE_ERROR: int = 4


def _tool_absence_detail(raw_tail: str, stderr_text: str) -> str:
    """Explain a pytest usage error in terms the caller can act on."""
    combined = f"{raw_tail}\n{stderr_text}"
    if "unrecognized arguments: -n" in combined or "xdist" in combined:
        return (
            "pytest rejected verify's -n auto even though pytest-xdist was "
            "importable in the container -- the installed xdist is broken or "
            "incompatible with this pytest. This is a tooling problem in the "
            "container, not a test result."
        )
    return (
        "pytest rejected verify's command line (usage error). This is a tooling "
        "problem in the container, not a test result."
    )


# ---------------------------------------------------------------------------
# Affected-scope test selection support (Issue #781)
# ---------------------------------------------------------------------------
#
# ``test_scope="affected"`` runs only the tests the change set touches.  The
# diff data below (numstat + name-status, staged and unstaged) is collected
# in the same exec calls as the diff summary so the exec count per verify
# call stays identical; the name-status half is split off the combined
# output with a marker line.

#: Marker separating ``--numstat`` output from ``--name-status`` output in
#: one combined ``git diff`` exec (see :func:`_split_ns`).
_NS_MARKER: str = "__SUNABA_NAMESTATUS__"


def _split_ns(raw: str) -> tuple[str, str]:
    """Split combined ``--numstat`` / ``--name-status`` output.

    The verify diff collection runs ``git diff ... --numstat`` and
    ``git diff ... --name-status`` in one exec, joined by a marker line, so
    the numstat half keeps its existing exec call count.  Returns
    ``(numstat_text, namestatus_text)``; a missing marker means the whole
    output is numstat (the pre-#781 interpretation).
    """
    marker = f"\n{_NS_MARKER}\n"
    if marker in raw:
        numstat, namestatus = raw.split(marker, 1)
        return numstat, namestatus
    if raw.startswith(_NS_MARKER + "\n"):
        return "", raw[len(_NS_MARKER) + 1:]
    return raw, ""


def _parse_name_status(raw: str) -> list[str]:
    """Collect deleted/renamed paths from ``git diff --name-status`` output.

    A deleted file (``D``) and both sides of a rename (``R``) mean the
    change set is not a plain edit of existing files: affected selection
    widens to the full suite for those (fail open).
    """
    special: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0].strip()
        if not code:
            continue
        if code[0] == "D":
            special.append(parts[1])
        elif code[0] == "R" and len(parts) > 2:
            special.append(parts[1])
            special.append(parts[2])
    return special


def _compute_diff_hash(
    unstaged_files: list[dict],
    staged_files: list[dict],
    untracked_files: list[str],
) -> str:
    """Stable hash over the sorted changed paths + per-file add/del counts.

    The same change set always yields the same hash (in full and affected
    modes), so journal analysis can pair an affected-green run with the
    subsequent full run of the same change set.
    """
    import hashlib

    per_path: dict[str, str] = {}
    for files in (unstaged_files, staged_files):
        for f in files:
            per_path.setdefault(
                f["path"], f"{f.get('additions', 0)}:{f.get('deletions', 0)}"
            )
    for p in untracked_files:
        per_path.setdefault(p, "new")
    lines = sorted(f"{p}:{v}" for p, v in per_path.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _empty_test_selection() -> dict:
    """test_selection shape for error results (no diff data collected).

    Validation errors are returned before any diff collection, so they
    carry an empty selection and a null diff_hash instead of omitting the
    fields entirely -- journal consumers can rely on the keys existing.
    """
    return {
        "changed_files": [],
        "selected_count": 0,
        "selection_ms": 0,
        "widened_to_full_reason": None,
        "mode": "full",
    }


def _test_failure_reason(test_result: dict, label: str = "tests") -> str:
    """Gate reason for a suite that ran and failed.

    A runner that cannot report counts -- ``npm test`` whose output is
    not TAP, for one -- returns the failure without a ``failed`` key.
    Defaulting that to 0 prints ``tests: 0 failure(s)`` as the reason the
    gate went red, which reads as "nothing failed" (Issue #857).  Say the
    count is unknown instead, and carry the runner's raw output so the
    failure is still diagnosable.
    """
    failed = test_result.get("failed")
    if isinstance(failed, int) and not isinstance(failed, bool):
        return f"{label}: {failed} failure(s)"

    msg = f"{label} ran and failed (failure count unavailable)"
    raw = test_result.get("raw_output") or test_result.get("error") or ""
    if raw:
        msg += f"\n{raw}"
    return msg


def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. note::

       This function is **no longer registered as an MCP tool** (see
       issue #256).  It remains available as an internal helper for
       machine-generated diffs.  For AI-authored edits, use
       :func:`edit_file` with ``old_str`` or
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
    hidden: bool = False,
    no_ignore: bool = False,
) -> str:
    """Search in the container with ripgrep (lexical) or ast-grep (structural).

    Dotfiles and ``.gitignore``d paths are excluded by default -- a pattern
    only in such files returns no matches, with no warning.  ``hidden=True``
    / ``no_ignore=True`` (lexical only) include them; ``.git/`` stays excluded.

    Args:
        container_id: Container ID prefix.
        pattern: Regex or AST pattern.
        path: File/dir to search; default auto-detects repo root.
        mode: 'lexical' (rg, grep fallback) or 'structural' (ast-grep).
        max_results: Result cap.
        glob: File filter (e.g. '*.py').
        ignore_case: Case-insensitive.
        context: Context lines per match.
        output_mode: 'content', 'files_with_matches', or 'count'.
        offset: Pagination offset.
        hidden: Search dotfiles; ``.git/`` stays excluded.
        no_ignore: Ignore ``.gitignore`` rules.

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

    # Reject paths that would trigger full-filesystem scan (Issue #744)
    if is_path_denied(resolved_path):
        return json.dumps({
            "status": "error",
            "error": (
                f"path \"{resolved_path}\" is denied. Full-filesystem scans are "
                "prohibited. To check whether a tool exists, "
                "use `list_files` or `sandbox_exec` instead."
            ),
        })

    record_tool_use(
        container_id[:12],
        "search_in_container",
        {"pattern": pattern, "path": resolved_path, "mode": mode},
    )
    results = search_files(
        client, container_id, pattern, path=resolved_path, mode=mode,
        max_results=max_results, glob=glob, ignore_case=ignore_case,
        context=context, output_mode=output_mode, offset=offset,
        # Forwarded only when set: search_files' defaults are the same, and
        # delegation tests assert the exact kwarg set for default calls.
        **({"hidden": hidden} if hidden else {}),
        **({"no_ignore": no_ignore} if no_ignore else {}),
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


def _record_verify_outcome(container_id: str, result: dict) -> None:
    """Record the verify outcome in the journal for the phase view (#774).

    ``verify_in_container`` runs tests via Docker SDK directly rather than
    through ``sandbox_exec``, so the journal would otherwise contain no
    pytest pass/fail entries for the most common verification path.

    This function writes a ``tool_use`` entry with tool name
    ``"verify_in_container"`` carrying a ``result`` key in params so the
    phase aggregator in ``src/sunaba/phase.py`` can populate the
    ``verify_timeline`` with actual pass/fail data.
    """
    outcome: dict = {"gate_passed": result.get("gate_passed", False)}
    tests = result.get("tests", {})
    full = tests.get("full", {})
    if isinstance(full, dict):
        outcome["passes"] = full.get("passed", full.get("passes", 0))
        outcome["fails"] = full.get("failed", full.get("fails", 0))
        outcome["collected"] = full.get("collected", 0)
        outcome["status"] = full.get("status", "unknown")
    elif isinstance(full, list):
        outcome["status"] = "multi_lang"
    record_tool_use(
        container_id[:12],
        "verify_in_container",
        {"result": outcome},
    )


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
    test_scope: str = "full",
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

    **test_scope="affected"** runs only the tests selected from the
    change set (fast edit-loop feedback) but NEVER passes the gate
    (``gate_passed`` false, ``partial_test_run`` true, success
    unrecorded) -- a full verify (default scope) is still required
    before publish.  Unnarrowable change sets (config/conftest,
    deletions, non-.py, non-Python, selector failure) widen to full;
    reason: ``test_selection.widened_to_full_reason``.  Full and
    affected runs carry ``test_selection`` and a stable ``diff_hash``;
    errors carry an empty selection, null hash.

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
        test_scope: 'full' (default) or 'affected'; 'affected' runs only
            the tests selected from the change set and never passes the
            gate, and is incompatible with test_filter/pytest_args.

    Returns:
        JSON: gate_passed, lint, types, patch_targets,
        lint_type_incomplete, partial_test_run, detected_languages,
        tests, diff_summary, gate_fail_reasons, test_selection,
        diff_hash.
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
        {
            "path": path,
            "test_filter": test_filter,
            "verbose": verbose,
            "test_scope": test_scope,
        },
    )

    # --- Validate test_scope before any exec work (Issue #781 review) ---
    # Invalid scopes and the test_filter/pytest_args conflict are detected
    # statically from the arguments; failing fast avoids the wasted
    # language-detection and diff-collection execs.  The error results
    # still carry test_selection (empty) and diff_hash (null) so journal
    # consumers can rely on the keys existing.
    if test_scope not in ("full", "affected"):
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": (
                f"invalid test_scope {test_scope!r}: expected 'full' or 'affected'"
            ),
            "diff_hash": None,
            "test_selection": _empty_test_selection(),
        })
    if test_scope == "affected" and (test_filter or pytest_args):
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": (
                "test_scope='affected' cannot be combined with test_filter or "
                "pytest_args (conflicting intent): affected mode selects the "
                "tests itself from the change set. Run them separately, or "
                "use the default test_scope='full'."
            ),
            "diff_hash": None,
            "test_selection": _empty_test_selection(),
        })

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

    def _run_affected_selector(
        repo_root: str,
        changed: list[str],
        deleted_or_renamed: list[str],
    ) -> dict:
        """Stage and run the stdlib-only affected-test selector (Issue #781).

        The selector source ships inside the sunaba package and is written
        into the container at verify time (temp path), then executed with
        the container's ``python3``.  It must NOT rely on the target repo
        containing the script, and it must NOT require network -- both hold:
        the source comes from the server's own install, and exec runs
        locally inside the container.

        Returns ``{ok, selected, widen_reason, selection_ms, error}``.
        """
        import time
        from pathlib import Path

        import sunaba.edit_verify.affected_tests as _affected_tests
        from sunaba.edit_verify import write_file

        try:
            source = Path(_affected_tests.__file__).read_bytes().decode("utf-8")
        except Exception as e:
            return {
                "ok": False, "selected": [], "widen_reason": None,
                "selection_ms": 0, "error": f"cannot read selector source: {e}",
            }
        selector_path = "/tmp/sunaba_affected_tests.py"
        try:
            write_file(container, container_id[:12], selector_path, source)
        except Exception as e:
            return {
                "ok": False, "selected": [], "widen_reason": None,
                "selection_ms": 0, "error": f"cannot stage selector: {e}",
            }
        cmd = f"python3 {shlex.quote(selector_path)} --root {shlex.quote(repo_root)}"
        for p in deleted_or_renamed:
            cmd += f" --deleted {shlex.quote(p)}"
        for p in changed:
            cmd += f" {shlex.quote(p)}"
        t0 = time.monotonic()
        ec, stdout_text, stderr_text = _run(cmd)
        selection_ms = int((time.monotonic() - t0) * 1000)
        if ec != 0:
            return {
                "ok": False, "selected": [], "widen_reason": None,
                "selection_ms": selection_ms,
                "error": (
                    f"selector exited {ec}: "
                    f"{stderr_text.strip() or stdout_text.strip()[:200]}"
                ),
            }
        try:
            parsed = json.loads(stdout_text.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError, ValueError):
            return {
                "ok": False, "selected": [], "widen_reason": None,
                "selection_ms": selection_ms,
                "error": f"selector produced invalid output: {stdout_text.strip()[:200]}",
            }
        return {
            "ok": True,
            "selected": list(parsed.get("selected") or []),
            "widen_reason": parsed.get("widen_reason"),
            "selection_ms": selection_ms,
        }

    # --- Get diff summary (structured JSON, issue #500, #687) ---
    # The name-status half is chained into the same exec calls (split by
    # _NS_MARKER) so the exec count per verify call is unchanged: deleted /
    # renamed files are detectable for affected-scope selection (#781).
    unstaged_ec, unstaged_raw, _ = _run(
        "git diff HEAD --numstat 2>/dev/null; "
        f"echo {_NS_MARKER}; git diff HEAD --name-status 2>/dev/null"
    )
    staged_ec, staged_raw, _ = _run(
        "git diff --cached --numstat 2>/dev/null; "
        f"echo {_NS_MARKER}; git diff --cached --name-status 2>/dev/null"
    )
    _, untracked_raw, _ = _run(
        "git ls-files --others --exclude-standard"
    )
    untracked_files = [
        f for f in untracked_raw.split("\n") if f.strip()
    ]

    unstaged_numstat, unstaged_ns = _split_ns(unstaged_raw)
    staged_numstat, staged_ns = _split_ns(staged_raw)
    deleted_or_renamed_paths = list(dict.fromkeys(
        _parse_name_status(unstaged_ns) + _parse_name_status(staged_ns)
    ))

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
        "unstaged": _build_diff_section(unstaged_numstat),
        "staged": _build_diff_section(staged_numstat),
        "untracked": untracked_files,
    }

    # --- Change set for affected-scope selection (Issue #781) ---
    changed_paths: list[str] = []
    for section in (diff_summary["unstaged"], diff_summary["staged"]):
        for f in section.get("files", []):
            p = f.get("path")
            if p and p not in changed_paths:
                changed_paths.append(p)
    for p in diff_summary["untracked"]:
        if p not in changed_paths:
            changed_paths.append(p)

    # Stable hash over the change set, identical across full and affected
    # runs so journal analysis can pair an affected-green run with the
    # subsequent full run of the same change set (#781).
    diff_hash = _compute_diff_hash(
        diff_summary["unstaged"].get("files", []),
        diff_summary["staged"].get("files", []),
        diff_summary["untracked"],
    )

    test_selection: dict = {
        "changed_files": sorted(changed_paths),
        "selected_count": 0,
        "selection_ms": 0,
        "widened_to_full_reason": None,
        "mode": "full",
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
        "diff_hash": diff_hash,
        "test_selection": test_selection,
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
            _record_verify_outcome(container_id, result)
            return json.dumps(result)

    # --- Run tests (language-aware dispatch, Issue #493) ---
    def _run_inline_pytest(filter_args: str, targets: str | list[str] | None = None) -> dict:
        """Run pytest inline (kept for python-specific error detail).

        *targets* (a list of test paths) replaces the *path* argument:
        affected-mode selections are passed to pytest as positional path
        arguments, never via ``-k`` (a file path in ``-k`` matches no
        tests -- see workflow_guide.md).
        """
        from sunaba.test_report import (
            PytestAdapter,
            build_pytest_cmd,
            split_pytest_output,
        )
        _junit_file = "/tmp/_pytest_report.xml"
        _raw_file = "/tmp/_pytest_raw.txt"
        target = targets if targets is not None else path
        full_cmd = build_pytest_cmd(_junit_file, _raw_file, filter_args, target, _SANDBOX_ENV)
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

        xml_part, raw_tail = split_pytest_output(stdout_text)

        if not xml_part:
            if ("No module named pytest" in raw_tail
                    or "No module named pytest" in stderr_text):
                return {"status": "not_available", "error": "pytest not installed",
                        "raw_output": raw_tail}
            if ec == 0:
                return {"status": "no_tests", "error": "no test output produced",
                        "raw_output": raw_tail}
            # pytest exited non-zero *and* produced no report: it crashed or was
            # killed.  Reporting that as "no tests" would launder a broken run
            # into a benign verdict -- the exact failure mode #584 was made of.
            return {"status": "error",
                    "error": f"pytest produced no XML report (exit {ec})",
                    "raw_output": raw_tail}

        try:
            report = PytestAdapter.parse(xml_part)
            d = report.to_dict()
            d["collected"] = report.total if report.total is not None else 0
            d["collection_errors"] = report.errors
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

    # --- Affected-scope test selection (Issue #781) ---
    # test_scope="affected" runs ONLY the tests the change set touches,
    # passed to pytest as positional paths.  It never reports
    # gate_passed=true and never records a verify success: the full suite
    # is still required for the publish gate.  When the change set cannot
    # be narrowed confidently the selector widens to full, and the run
    # below the branch is then a genuine full run with normal gate
    # semantics (the reason is recorded in test_selection).
    if test_scope == "affected":
        test_selection["mode"] = "affected"
        widen_reason: str | None = None
        selected: list[str] | None = None
        if set(detected.languages) != {"python"}:
            widen_reason = (
                "affected test selection requires exactly {python}; "
                f"detected {sorted(detected.languages)}"
            )
        elif not changed_paths and not deleted_or_renamed_paths:
            widen_reason = "no changed files detected"
        else:
            selection = _run_affected_selector(
                working_dir, changed_paths, deleted_or_renamed_paths
            )
            test_selection["selection_ms"] = selection["selection_ms"]
            if not selection["ok"]:
                widen_reason = f"affected-test selector failed: {selection['error']}"
            elif selection["widen_reason"]:
                widen_reason = selection["widen_reason"]
            else:
                selected = selection["selected"]
                test_selection["selected_count"] = (
                    len(selected) if selected is not None else 0
                )
                if not selected:
                    widen_reason = "affected-test selector returned no tests"

        if widen_reason is not None:
            test_selection["widened_to_full_reason"] = widen_reason
        else:
            # Affected run: ONLY the selected tests, positionally.
            affected_result = _run_inline_pytest("", targets=selected)
            result["tests"]["full"] = affected_result
            result["partial_test_run"] = True
            result["gate_passed"] = False
            result["gate_skipped_reason"] = (
                "test_scope='affected': only affected tests ran; the full "
                "suite is still required for the gate"
            )
            result["recommended_next_action"] = (
                "verify_in_container with default test_scope='full' "
                "(affected runs never pass the publish gate)"
            )
            status = affected_result.get("status", "unknown")
            if status == "collection_error":
                raw = affected_result.get("raw_output", "")
                msg = (
                    "affected tests collection error: "
                    f"{affected_result.get('error', 'unknown')}"
                )
                if raw:
                    msg += f"\n{raw}"
                result["gate_fail_reasons"] = [msg]
            elif status == "not_available":
                result["gate_fail_reasons"] = [
                    "affected tests not available: "
                    f"{affected_result.get('error', 'unknown')}"
                ]
            elif status == "no_tests":
                result["gate_fail_reasons"] = [
                    "affected test selection matched no tests"
                ]
            elif status == "error":
                result["gate_fail_reasons"] = [
                    f"test execution error: {affected_result.get('error', 'unknown')}"
                ]
            elif status != "ok":
                result["gate_fail_reasons"] = [
                    _test_failure_reason(affected_result, "affected tests")
                ]
            _record_verify_outcome(container_id, result)
            return json.dumps(result)

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
                _record_verify_outcome(container_id, result)
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
        _record_verify_outcome(container_id, result)
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
            result["gate_fail_reasons"] = [_test_failure_reason(full_result)]
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
                    reasons.append(_test_failure_reason(lr, lang))
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
    # Nudge toward publish on success: publish now hard-blocks without
    # a recorded verify pass (Issue #615), so the agent benefits from
    # the hint (Issue #619).
    if result["gate_passed"]:
        record_verify_success(container_id)
        result["recommended_next_action"] = "publish"

    # Record the verify outcome in the journal so the trace-page phase
    # view can render pass/fail checkmarks without relying on pytest
    # exec entries (which verify_in_container does not produce — it
    # runs tests via Docker SDK directly, bypassing sandbox_exec).
    # Issue #774.
    _record_verify_outcome(container_id, result)
    return json.dumps(result)
