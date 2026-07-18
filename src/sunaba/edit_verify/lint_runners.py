"""Lint and type-check runners for verification."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .jstools import _annotate_resolution, _resolve_js_tool
from .parsers import (
    _determine_lint_severity,
    _parse_eslint_output,
    _parse_go_vet_output,
    _parse_golangci_lint_output,
    _parse_pyright_output,
    _parse_ruff_output,
    _parse_tsc_json,
    _parse_tsc_text,
)
from .results import (
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_ok,
)
from .shell import _GO_ENV, _SANDBOX_ENV, _path_display, _quote_path

# ---------------------------------------------------------------------------
# Linter / Type checker / Test / Scan runners
# ---------------------------------------------------------------------------
# Each runner now returns a VerifyResult envelope.  The ``|| true`` and
# ``2>/dev/null`` silencing has been removed: exit codes are inspected
# directly, and stderr is captured (not discarded).
#
# Runner return semantics:
# - exit 0   + output -> status "findings" (parse output)
# - exit 0   + no output -> status "ok" (clean)
# - exit 1   (many tools use this for "findings") -> status "findings"
# - exit 127             -> status "not_available"
# - exit other           -> status "error" (unexpected failure)
# - "skipped" is only for intentional non-execution (e.g. go type layer)


_RUFF_SECURITY_SELECT = ",".join([
    # shell injection
    "S102", "S602", "S603", "S604", "S605", "S606", "S607",
    # eval / exec
    "S307",
    # deserialization
    "S301", "S302", "S506",
    # TLS / SSL
    "S501", "S502", "S503", "S504",
    # weak hash
    "S324",
    # XML (XXE)
    "S313", "S314", "S315", "S316", "S317", "S318", "S319",
    # network safety
    "S113", "S507",
    # template injection
    "S701",
])

_RUFF_SECURITY_IGNORE = ",".join([
    # S101: assert is idiomatic in pytest and common for invariant guards in
    # application code (e.g. `assert x is not None`). Excluding it avoids
    # flooding test suites; the trade-off is that non-test assert-as-guard
    # patterns are not flagged. Acceptable because LLMs can reason about
    # assert usage from context without a dedicated lint signal.
    "S101",
    "S105", "S106", "S107",  # hardcoded-password heuristics — high false-positive rate
    "S311",          # random — usually non-security
    "S110", "S112",  # try-except-pass / try-except-continue — style, not security
])

def _run_ruff_verify(
    container: Any,
    path: str | Sequence[str],
    workdir: str | None = None,
    extra_select: bool = True,
    fix: bool = False,
) -> VerifyResult:
    """Run ruff on *path*.  Returns VerifyResult envelope.

    When *extra_select* is ``True`` (default) the curated security
    rule-set is layered on top of the project's own ruff config for
    awareness during editing.  Pass ``extra_select=False`` to run ruff
    with the project config **only** -- this mirrors CI's plain
    ``ruff check`` exactly and is what the pre-test gate uses, so the
    gate never diverges from CI on rules the project hasn't opted into.

    When *fix* is ``True`` ruff is invoked with ``--fix`` so it applies
    its safe autofixes (import sorting, unused-import removal, etc.) to
    *path* in place; the returned findings are the violations that
    remain *after* fixing (Issue #284).
    """
    # _quote_path uses shlex.quote (single-quote wrapping), so paths with
    # spaces or special characters are safe. SELECT/IGNORE are comma-separated
    # rule codes with no whitespace, so no quoting is needed for those.
    security_args = (
        f"--extend-select {_RUFF_SECURITY_SELECT} "
        f"--extend-ignore {_RUFF_SECURITY_IGNORE} "
        if extra_select
        else ""
    )
    fix_arg = "--fix " if fix else ""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}ruff check --output-format json "
            f"{fix_arg}"
            f"{security_args}"
            f"{_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("ruff", "ruff not installed in container")
    if ec not in (0, 1):
        return _envelope_error("ruff", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_ruff_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _envelope_ok("ruff", findings, ec)


def _run_eslint_verify(
    container: Any, path: str | Sequence[str], workdir: str | None = None, fix: bool = False
) -> VerifyResult:
    """Run eslint on *path*.  Returns VerifyResult envelope.

    When *fix* is ``True`` eslint is invoked with ``--fix`` so it
    rewrites *path* in place; the returned findings are the problems
    that remain *after* fixing (Issue #284).

    Resolves ``node_modules/.bin/eslint`` before the image-baked global
    (Issue #588) so a repo pinned to a different eslint major never
    silently gets linted by the wrong version; the envelope's ``detail``
    always says which one ran.
    """
    fix_arg = "--fix " if fix else ""
    cmd, source = _resolve_js_tool(container, "eslint", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} {fix_arg}--format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("eslint", "eslint not installed in container"), source, cmd
        )
    if ec not in (0, 1, 2):
        # eslint exit 2 = runtime error
        return _annotate_resolution(
            _envelope_error("eslint", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_eslint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = _determine_lint_severity(r.get("rule", ""))
    return _annotate_resolution(_envelope_ok("eslint", findings, ec), source, cmd)


def _run_golangci_lint_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run golangci-lint on *path*.  Falls back to go vet."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}golangci-lint run --out-format json {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    if ec == 127:
        return _run_go_vet_verify(container, path)

    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec not in (0, 1):
        # golangci-lint uses exit 2 for execution errors (config issues, etc.)
        return _envelope_error("golangci-lint", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_golangci_lint_output(stdout_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("golangci-lint", findings, ec)


def _run_go_vet_verify(container: Any, path: str | Sequence[str]) -> VerifyResult:
    """Run go vet on *path*."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go vet {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("go vet", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go vet", stderr_text.strip() or f"exit code {ec}", ec)

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_go_vet_output(stdout_text + "\n" + stderr_text, _path_display(path))
    for r in findings:
        r["severity"] = "error"
    return _envelope_ok("go vet", findings, ec)


def _run_pyright_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run pyright on *path*.  Returns VerifyResult envelope."""
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}pyright --outputjson {_quote_path(path)}",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if ec == 127:
        return _envelope_not_available("pyright", "pyright not installed in container")

    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    findings = _parse_pyright_output(stdout_text, path)
    for r in findings:
        r["severity"] = "error"

    if ec not in (0, 1) and not findings:
        return _envelope_error("pyright", stderr_text.strip() or f"exit code {ec}", ec)

    return _envelope_ok("pyright", findings, ec)


def _run_tsc_verify(container: Any, path: str, workdir: str | None = None) -> VerifyResult:
    """Run tsc --noEmit on *path*.  Returns VerifyResult envelope.

    Resolves ``node_modules/.bin/tsc`` before the image-baked global
    (Issue #588); the envelope's ``detail`` always says which one ran.
    Invokes the resolved binary directly instead of ``npx`` so the
    resolution is explicit and identical across eslint/tsc/jest, rather
    than relying on npx's own (differently-behaved) fallback search.
    """
    cmd, source = _resolve_js_tool(container, "tsc", workdir=workdir)
    ec, output = container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --noEmit {_quote_path(path)} 2>&1",
        ],
        stdout=True,
        stderr=True,
        workdir=workdir,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    combined = ""
    if stdout_part:
        combined += stdout_part.decode("utf-8", errors="replace")
    if stderr_part:
        combined += stderr_part.decode("utf-8", errors="replace")

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("tsc", "typescript (tsc) not installed in container"),
            source, cmd,
        )
    if ec not in (0, 1, 2):
        return _annotate_resolution(
            _envelope_error("tsc", combined.strip() or f"exit code {ec}", ec), source, cmd
        )

    findings = _parse_tsc_text(combined, path)
    if not findings:
        findings = _parse_tsc_json(combined, path)
    for r in findings:
        r["severity"] = "error"
    return _annotate_resolution(_envelope_ok("tsc", findings, ec), source, cmd)
