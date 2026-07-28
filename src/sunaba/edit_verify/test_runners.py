"""Test runners and unified dispatch table for verification."""

from __future__ import annotations

import json
import shlex
from typing import Any

from .jstools import _annotate_resolution, _detect_js_test_runner, _resolve_js_tool
from .lint_runners import (
    _resolve_cargo_manifest,
    _run_clippy_verify,
    _run_eslint_verify,
    _run_golangci_lint_verify,
    _run_pyright_verify,
    _run_ruff_verify,
    _run_rust_type_verify,
    _run_tsc_verify,
)
from .results import (
    VerifyResult,
    _envelope_error,
    _envelope_not_available,
    _envelope_skipped,
)
from .shell import _GO_ENV, _RUST_ENV, _SANDBOX_ENV, _exec_run, _quote_path


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
    ec, stdout_text, stderr_text = _exec_run(
        container,
        ["/bin/sh", "-c", cmd],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("pytest", "python3 not found in container")
    if ec == 2:
        _, raw_tail = split_pytest_output(stdout_text)
        detail = "test collection failed"
        if raw_tail:
            detail += f"\n{raw_tail}"
        return _envelope_error("pytest", detail, ec)
    if ec == 5:
        return _envelope_skipped("pytest", "no tests found")
    if ec not in (0, 1):
        return _envelope_error("pytest", stderr_text.strip() or f"exit code {ec}", ec)

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
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{cmd} --json --passWithNoTests {_quote_path(path)}",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _annotate_resolution(
            _envelope_not_available("jest", "jest not installed in container"), source, cmd
        )
    if ec not in (0, 1):
        return _annotate_resolution(
            _envelope_error("jest", stderr_text.strip() or f"exit code {ec}", ec), source, cmd
        )

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
    ec, stdout_text, _ = _exec_run(
        container,
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}cat package.json 2>/dev/null"],
        workdir=workdir,
    )

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
    ec, combined, _ = _exec_run(
        container,
        ["/bin/sh", "-c", f"{_SANDBOX_ENV}npm test --silent 2>&1"],
        workdir=workdir,
    )

    # 4. Try to parse TAP v13 counts (node --test and similar runners)
    tap_report = None
    try:
        from sunaba.test_report import TapAdapter

        tap_report = TapAdapter.parse_json(combined)
    except Exception:
        tap_report = None

    output_tail = (
        "\n".join(combined.strip().split("\n")[-20:]) if combined.strip() else ""
    )

    if tap_report is not None:
        # TAP output parsed successfully — embed counts in detail.
        d = tap_report.to_dict()
        status = d.get("status", "ok")
        if ec != 0 and output_tail:
            d["raw_tail"] = output_tail
        return VerifyResult(
            tool="npm test",
            status="ok" if status == "ok" else "findings",
            detail=json.dumps(d),
            exit_code=ec,
        )

    # 5. Cannot parse TAP output.
    if ec == 0:
        # Exit code 0 but output is not parseable TAP.  The caller can
        # distinguish this from a run-with-tests by the absence of
        # ``total`` in *detail* (see issue #738).
        return VerifyResult(
            tool="npm test",
            status="ok",
            detail=json.dumps(
                {
                    "status": "ok",
                    "note": (
                        "npm test exited 0 but its output is not TAP; "
                        "test counts are unavailable, so this result "
                        "does not attest that any test ran."
                    ),
                }
            ),
            exit_code=ec,
        )

    # 6. Non-zero, unparseable: discriminate not_available vs findings
    #    Conservative matching: only known "runner missing" strings
    #    produce not_available; everything else is a test failure.
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
    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_GO_ENV}go test -json {_quote_path(path)}",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("go test", "go not installed in container")
    if ec not in (0, 1):
        return _envelope_error("go test", stderr_text.strip() or f"exit code {ec}", ec)

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


def _run_cargo_test_verify(
    container: Any, path: str, workdir: str | None = None
) -> VerifyResult:
    """Run cargo test on *path*.  Returns VerifyResult envelope.

    Stable cargo has no structured (JSON) test-report format -- unlike
    ``go test -json``, ``cargo test``'s own ``--format json`` sits behind
    ``-Z unstable-options`` (nightly only) -- so this parses cargo's
    plain per-binary text summary via
    :class:`~sunaba.test_report.RustTestAdapter` instead.

    *path* is accepted for interface parity with the other test runners
    (the dispatch table calls every entry the same way:
    ``runner(container, test_path, workdir=working_dir)``) but is not
    passed to cargo, for the same reason it is unused in
    :func:`~sunaba.edit_verify.lint_runners._run_clippy_verify`: cargo
    test has no path/package positional argument that scopes the run to
    a subdirectory the way ``go test``'s package pattern does.
    ``--workspace`` runs every member crate's tests (including for a
    workspace-only root manifest with no ``[package]``), and
    ``--no-fail-fast`` keeps a single early failure from hiding every
    other test's result -- the same reason golangci-lint/pytest/jest
    runners here report the full result set rather than stopping at the
    first problem.

    Output is merged with ``2>&1`` (mirroring ``_run_npm_test_verify``'s
    handling of another non-JSON, human-text test runner) since cargo's
    build/compile progress goes to stderr while the ``test result: ...``
    summary lines are not reliably confined to stdout across wrapper
    invocations; the adapter regexes only care about matching text, not
    which stream it arrived on.
    """
    manifest = _resolve_cargo_manifest(container, path, workdir=workdir)
    if manifest is None:
        probe_ec, _, _ = _exec_run(
            container, ["/bin/sh", "-c", "command -v cargo"], workdir=workdir
        )
        if probe_ec != 0:
            return _envelope_not_available("cargo test", "cargo not installed in container")
        return _envelope_error(
            "cargo test", f"no Cargo.toml found under {workdir or 'the working directory'}", 1
        )

    ec, stdout_text, stderr_text = _exec_run(
        container,
        [
            "/bin/sh",
            "-c",
            f"{_SANDBOX_ENV}{_RUST_ENV}cargo test --workspace "
            f"--manifest-path {shlex.quote(manifest)} --no-fail-fast 2>&1",
        ],
        workdir=workdir,
    )

    if ec == 127:
        return _envelope_not_available("cargo test", "cargo not installed in container")
    if ec not in (0, 101):
        return _envelope_error("cargo test", stderr_text.strip() or f"exit code {ec}", ec)

    if not stdout_text.strip():
        return _envelope_skipped("cargo test", "no test output produced")

    try:
        from sunaba.test_report import RustTestAdapter

        report = RustTestAdapter.parse_json(stdout_text)
        d = report.to_dict()
        status = d.get("status", "ok")
        return VerifyResult(
            tool="cargo test",
            status="findings" if status == "failed" else "ok",
            findings=[],
            detail=json.dumps(d),
            exit_code=ec,
        )
    except Exception:
        detail = "failed to parse cargo test output"
        if stdout_text.strip():
            tail = "\n".join(stdout_text.strip().split("\n")[-20:])
            detail += f"\n--- raw output tail ---\n{tail}"
        return _envelope_error("cargo test", detail, ec)


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
    "rust": {
        "lint": _run_clippy_verify,
        # Unlike go's bare `None` (generic "no type layer" fallback),
        # rust points at a dedicated function so the disposition is a
        # specific, visible reason rather than a one-size-fits-all
        # message -- see _run_rust_type_verify's docstring.
        "type": _run_rust_type_verify,
        "test": _run_cargo_test_verify,
    },
    "unknown": {
        "lint": None,
        "type": None,
        "test": None,
    },
}
