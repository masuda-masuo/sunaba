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

import json
import re
from collections.abc import Sequence
from typing import Any

# Import extracted submodules
from .detect import (  # noqa: F401
    _DETECTION_MARKERS,
    _EXCLUDE_DIRS,
    _LANGUAGE_EXT_MAP,
    DetectionResult,
    _find_tsconfig_upward,
    detect_languages,
)
from .edits import (  # noqa: F401
    _normalize_diff_for_git,
    apply_patch_to_file,
    edit_symbol_in_container,
    transform_file_in_container,
)
from .fileio import (  # noqa: F401
    _compute_file_size,
    _file_size_from_counts,
    _owner_for_write,
    read_file,
    read_file_lines,
    write_file,
)

#: Environment variables to set before running linters/type checkers
#: inside sandbox containers.  Containers run as a non-root user with
#: a read-only ``/``, so cache directories must point to ``/tmp``.
# Import extracted submodules
from .jstools import (  # noqa: F401
    _annotate_resolution,
    _detect_js_test_runner,
    _resolve_js_tool,
)
from .lint_runners import (  # noqa: F401
    _RUFF_SECURITY_IGNORE,
    _RUFF_SECURITY_SELECT,
    _run_eslint_verify,
    _run_go_vet_verify,
    _run_golangci_lint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
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
from .results import (  # noqa: F401
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
    _envelope_skipped,
)
from .shell import _GO_ENV, _SANDBOX_ENV, _path_display, _quote_path  # noqa: F401
from .test_runners import (  # noqa: F401
    _DISPATCH,
    _run_go_test_verify,
    _run_jest_verify,
    _run_npm_test_verify,
    _run_pytest_verify,
)

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
