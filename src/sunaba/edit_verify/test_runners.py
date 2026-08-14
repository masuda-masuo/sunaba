"""Test runners and unified dispatch table for verification."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
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
    """Run pytest --junit-xml on *path*.  Returns VerifyResult envelope.

    Uses pytest's built-in JUnit XML report (``--junit-xml``, Issue #785)
    -- no third-party plugin (pytest-json-report) is a prerequisite.

    *workdir* defaults to the container's own working directory, which is
    the repo root; pass it only to run somewhere else (e.g. a subproject).
    """
    from sunaba.test_report import (
        PytestAdapter,
        build_pytest_cmd,
        split_pytest_output,
    )
    _junit_file = "/tmp/_pytest_report.xml"
    _raw_file = "/tmp/_pytest_raw.txt"
    cmd = build_pytest_cmd(_junit_file, _raw_file, "", _quote_path(path), _SANDBOX_ENV)
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

    xml_part, raw_tail = split_pytest_output(stdout_text)

    if not xml_part:
        detail = "no test output produced"
        if raw_tail:
            detail += f"\n--- raw output ---\n{raw_tail}"
        return _envelope_skipped("pytest", detail)

    try:
        report = PytestAdapter.parse(xml_part)
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


@dataclass(frozen=True)
class _RunnerFailureRule:
    """One way to prove ``npm test`` never got as far as running the suite.

    *pattern* is matched against a single output **line**, never against
    the whole stream: ``npm test --silent 2>&1`` merges the suite's own
    printouts into the same buffer, so a blob-wide substring match reads
    a test's own assertion text as a runner diagnostic (Issue #857).

    *exit_codes* is the companion evidence.  A nonzero exit proves
    nothing on its own -- a suite that ran and failed exits nonzero too
    -- so a rule fires only on the conjunction of both, and only when
    the diagnostics dominate the stream (see _diagnostic_share): a line
    a suite printed among its own output is a quote, not a diagnostic.
    """

    name: str
    exit_codes: tuple[int, ...]
    pattern: re.Pattern[str]


# npm's own diagnostics are line-prefixed: ``npm error`` (npm >= 10) or
# ``npm ERR!`` (npm <= 9).  A test body can write those same bytes, so
# the prefix alone is not proof -- see _diagnostic_share below.
_NPM_DIAG_LINE = r"^\s*npm (?:error\b|ERR!)"

# A shell's exec diagnostic: "<who>: ... not found".  Matches
# "/bin/sh: 1: npm: not found" and "bash: npm: command not found", and
# also an assertion line that quotes one -- again, dominance decides.
_SHELL_NOT_FOUND = re.compile(r"^\s*\S+: .*\bnot found\b")

# Lines a genuine runner-absent stream may carry besides the diagnostic
# itself: npm's script banner (``> pkg@1.0.0 test``), which --silent
# suppresses, and blank lines.  Neither is evidence, neither dilutes it.
_NPM_BANNER_LINE = re.compile(r"^\s*>\s")

# Every line attributable to npm or the shell, at any log level.  Used
# to measure how much of the merged stream is diagnostic: a rule fires
# only when diagnostics *dominate*, not merely appear (Issue #857).
_NPM_DIAGNOSTIC_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*npm (?:error\b|ERR!|warn\b|WARN|notice\b)"),
    _SHELL_NOT_FOUND,
)

# Share of non-blank, non-banner lines that must be diagnostics.
_DIAGNOSTIC_DOMINANCE = 0.9

_NPM_RUNNER_FAILURE_RULES: tuple[_RunnerFailureRule, ...] = (
    # The shell could not exec npm, or npm could not exec the test tool.
    # 127 is the shell's own "not found" status and the message is the
    # shell's, not npm's.  Verified in this image (npm 10.9.8) under the
    # command this module runs: a missing npm gives
    # "/bin/sh: 1: npm: not found" and a missing test binary gives
    # "sh: 1: vitest: not found" -- in both cases the whole stream.
    _RunnerFailureRule(
        name="npm not found",
        exit_codes=(127,),
        pattern=_SHELL_NOT_FOUND,
    ),
    # npm ran, but package.json declares no such script.
    _RunnerFailureRule(
        name="npm script missing",
        exit_codes=(1,),
        pattern=re.compile(_NPM_DIAG_LINE + r".*Missing script:"),
    ),
    # npm itself failed around the script -- a missing binary or path
    # (``npm error code ENOENT``), registry or cache trouble, and so on.
    # Any npm-prefixed diagnostic qualifies.  Note --silent, which this
    # module passes, suppresses this block on npm 10.9.8 here, so under
    # sunaba's own command these lines usually arrive only from npm <= 9
    # or from a test echoing them -- hence the dominance requirement.
    _RunnerFailureRule(
        name="npm error",
        exit_codes=(1,),
        pattern=re.compile(_NPM_DIAG_LINE),
    ),
)


def _diagnostic_share(lines: list[str]) -> float:
    """Fraction of the stream that reads as an npm/shell diagnostic.

    Blank lines and npm's script banner are ignored: a genuine
    runner-absent run may contain them and they are not test output.
    Everything else counts, so a single diagnostic-looking line among a
    suite's own printouts scores low and cannot carry a rule.
    """
    considered = [
        line for line in lines
        if line.strip() and not _NPM_BANNER_LINE.match(line)
    ]
    if not considered:
        return 0.0
    hits = sum(
        1 for line in considered
        if any(p.search(line) for p in _NPM_DIAGNOSTIC_LINE_PATTERNS)
    )
    return hits / len(considered)


def _classify_npm_runner_failure(
    ec: int, combined: str
) -> tuple[str, str] | None:
    """Return ``(rule name, evidence line)`` when the runner never ran.

    Returns ``None`` when the evidence does not prove that -- including
    the case where the suite plainly ran and failed.  The default of
    doubt is "the suite ran": a misread there still leaves the gate red,
    while the reverse blames a missing runner for a real test failure.
    """
    lines = combined.splitlines()

    # ELIFECYCLE, when present, means npm reached the script and the
    # script itself exited nonzero -- so the suite ran.  Only npm <= 9
    # printed it: npm 10.9.8, the version in this image, was measured
    # here printing no error block at all on a lifecycle failure (an
    # exiting-127 test script leaves the merged stream empty and
    # propagates 127).  Kept for older npm output; nothing below relies
    # on it, which is why the dominance check exists.
    if any("ELIFECYCLE" in line for line in lines):
        return None

    # The suite writes into the same merged stream as npm, so a
    # diagnostic that shares the stream with test names and assertion
    # lines is most likely quoted *by* a test.  A genuine runner-absent
    # run has nothing else to print (Issue #857).
    if _diagnostic_share(lines) < _DIAGNOSTIC_DOMINANCE:
        return None

    for rule in _NPM_RUNNER_FAILURE_RULES:
        if ec not in rule.exit_codes:
            continue
        for line in lines:
            if rule.pattern.search(line):
                return rule.name, line.strip()
    return None


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

    # 6. Non-zero, unparseable: discriminate not_available vs findings.
    #    The evidence has to be a runner diagnostic *line* plus a
    #    matching exit code -- see _classify_npm_runner_failure.  A
    #    substring of the merged blob is not evidence: the suite writes
    #    into that same stream, so a test printing "ENOENT" used to be
    #    reported as a missing runner (Issue #857).
    runner_failure = _classify_npm_runner_failure(ec, combined)
    if runner_failure is not None:
        rule_name, evidence = runner_failure
        detail = f"npm test did not run ({rule_name}): {evidence}"
        if output_tail:
            detail += f"\n--- raw output tail ---\n{output_tail}"
        return _envelope_not_available("npm test", detail)

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
