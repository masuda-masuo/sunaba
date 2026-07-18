"""Test runners and unified dispatch table for verification."""

from __future__ import annotations

import json
from typing import Any

from .jstools import _annotate_resolution, _detect_js_test_runner, _resolve_js_tool
from .lint_runners import (
    _run_eslint_verify,
    _run_golangci_lint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_tsc_verify,
)
from .results import (
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
    _envelope_skipped,
)
from .shell import _GO_ENV, _SANDBOX_ENV, _quote_path


def _run_pytest_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run pytest --json-report on *path*.  Returns VerifyResult envelope.

    *workdir* defaults to the container's own working directory, which is
    the repo root; pass it only to run somewhere else (e.g. a subproject).
    """
    from sunaba.test_report import (
        PytestAdapter,
        build_pytest_cmd,
        split_pytest_output,
    )
    _json_file = "/tmp/_pytest_report.json"
    _raw_file = "/tmp/_pytest_raw.txt"
    cmd = build_pytest_cmd(_json_file, _raw_file, "", _quote_path(path), _SANDBOX_ENV)
    ec, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pytest", "python3 not found in container")
    if ec == 2:
        stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        _, raw_tail = split_pytest_output(stdout_text)
        detail = "test collection failed"
        if raw_tail:
            detail += f"\n{raw_tail}"
        return _envelope_error("pytest", detail, ec)
    if ec == 5:
        return _envelope_skipped("pytest", "no tests found")
    if ec not in (0, 1):
        return _envelope_error("pytest", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    json_part, raw_tail = split_pytest_output(stdout_text)

    if not json_part:
        detail = "no test output produced"
        if raw_tail:
            detail += f"\n--- raw output ---\n{raw_tail}"
        return _envelope_skipped("pytest", detail)

    try:
        report = PytestAdapter.parse_json(json_part)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="pytest",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        detail = "failed to parse pytest output"
        if raw_tail:
            detail += f"\n--- raw output ---\n{raw_tail}"
        return _envelope_error("pytest", detail, ec)


def _run_jest_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run jest --json on *path*.  Returns VerifyResult envelope.

    Discriminates jest vs vitest via ``package.json`` first (design §3,
    Issue #588): running the jest CLI against a vitest-only project would
    misparse vitest's own output as a crash rather than reporting the
    real gap honestly.  Resolves ``node_modules/.bin/jest`` before the
    image-baked global, same as eslint/tsc; the resolution is recorded
    in the envelope's ``detail`` (as JSON fields alongside the test
    report, since ``detail`` here is machine-parsed downstream).
    """
    runner = _detect_js_test_runner(container, workdir=workdir)
    if runner == "vitest":
        return _envelope_skipped(
            "jest",
            "package.json indicates vitest (no jest dependency); sunaba's "
            "js test dispatch only runs jest today -- no VitestAdapter yet "
            "(#588 follow-up)",
        )

    cmd, source = _resolve_js_tool(container, "jest", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --json --passWithNoTests {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("jest", "jest not installed in container"), source, cmd
        )
    if ec not in (0, 1):
        return _annotate_resolution(
            _envelope_error("jest", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _annotate_resolution(
            _envelope_skipped("jest", "no test output produced"), source, cmd
        )

    try:
        from sunaba.test_report import JestAdapter

        report = JestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        result = VerifyResult(
            tool="jest",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
        return _annotate_resolution(result, source, cmd)
    except Exception:
        detail = "failed to parse jest output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _annotate_resolution(_envelope_error("jest", detail, ec), source, cmd)


def _run_npm_test_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run ``npm test`` when ``package.json`` declares a ``scripts.test``.

    Reads the repo-root ``package.json``, checks for ``scripts.test``,
    and either delegates to ``npm test`` or falls back to
    :func:`_run_jest_verify` (the previous dispatch target).

    Returns a :class:`VerifyResult` envelope following the same status
    conventions as ``_run_go_test_verify``:
        - ``status="ok"`` on exit code 0.
        - ``status="findings"`` on non-zero exit (test failure).
        - ``status="not_available"`` when the runner/script is missing.
    """
    # 1. Read repo-root package.json
    ec, output = container.exec_run(
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}cat package.json 2>/dev/null"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    # 2. Parse & check for scripts.test
    scripts_test: str | None = None
    if stdout_text.strip():
        try:
            pkg = json.loads(stdout_text)
            scripts_test = pkg.get("scripts", {}).get("test")
        except (json.JSONDecodeError, AttributeError):
            scripts_test = None

    if not scripts_test:
        # Fall back to jest (historical behaviour)
        return _run_jest_verify(container, path, workdir=workdir)

    # 3. Run npm test
    ec, output = container.exec_run(
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}npm test --silent 2>&1"],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, _stderr_part = output if isinstance(output, tuple) else (output, b"")
    combined = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if ec == 0:
        return _envelope_ok("npm test", [], ec)

    # 4. Non-zero: discriminate not_available vs findings
    #    Conservative matching: only known "runner missing" strings
    #    produce not_available; everything else is a test failure.
    output_tail = "\n".join(combined.strip().split("\n")[-20:]) if combined.strip() else ""

    npm_error_no_lifecycle = (
        "npm error" in combined and "ELIFECYCLE" not in combined
    )
    if (
        "command not found" in combined
        or ": not found" in combined
        or "Missing script" in combined
        or "ENOENT" in combined
        or npm_error_no_lifecycle
    ):
        return _envelope_not_available("npm test", output_tail)

    return VerifyResult(
        tool="npm test",
        status="findings",
        detail=output_tail,
        exit_code=ec,
    )


def _run_go_test_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run go test -json on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go test -json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("go test", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go test", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""

    if not stdout_text.strip():
        return _envelope_skipped("go test", "no test output produced")

    try:
        from sunaba.test_report import GoTestAdapter

        report = GoTestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="go test",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        detail = "failed to parse go test output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _envelope_error("go test", detail, ec)


# ---------------------------------------------------------------------------
# Unified dispatch table
# ---------------------------------------------------------------------------
# Maps language -> layer -> runner function.
# Python type layer uses pyright.
# Go lint tries golangci-lint first, falls back to go vet.
# JS has no type layer (skipped).  Go type is covered by go vet/build.


_DISPATCH: dict[str, dict[str, Any]] = {
    "python": {
        "lint": _run_ruff_verify,
        "type": _run_pyright_verify,  # primary
        "test": _run_pytest_verify,
    },
    "js": {
        "lint": _run_eslint_verify,
        "type": None,  # skipped
        "test": _run_npm_test_verify,
    },
    "ts": {
        "lint": _run_eslint_verify,
        "type": _run_tsc_verify,
        "test": _run_npm_test_verify,
    },
    "go": {
        "lint": _run_golangci_lint_verify,
        "type": None,  # skipped: build/vet covers typing
        "test": _run_go_test_verify,
    },
    "unknown": {
        "lint": None,
        "type": None,
        "test": None,
    },
}
