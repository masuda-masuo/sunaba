"""Pre-test lint + type gate (Issue #293) and language-layer dispatch."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from .detect import detect_languages
from .lint_runners import (
    _run_eslint_verify,
    _run_golangci_lint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
from .results import VerifyResult, _envelope_not_available, _envelope_skipped
from .shell import _SANDBOX_ENV
from .test_runners import _DISPATCH

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
