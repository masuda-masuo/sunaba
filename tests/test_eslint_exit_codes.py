"""Tests for eslint exit-code handling in _run_eslint_verify (Issue #740).

Exit 2 means "the run itself failed" (no config, bad CLI usage, config
syntax error) -- stdout is typically empty and stderr carries the
reason.  The fix aligns eslint with the pyright pattern:
``if ec not in (0, 1) and not findings -> status='error'``.

This test file uses real eslint output shapes so the fixture is
meaningful.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sunaba.edit_verify import (
    VerifyResult,
    _run_eslint_verify,
    lint_file,
    run_lint_type_gate,
)

# ---------------------------------------------------------------------------
# ESLint real output shapes
# ---------------------------------------------------------------------------

# Actual eslint stderr when no config file is found (eslint >= 9.x).
_STDERR_NO_CONFIG = (
    "ESLint couldn't find an eslint.config.(js|mjs|cjs) file.\n\n"
    "ESLint stopped because it needs to find configuration to run.\n"
    "You can create one by running `eslint --init`.\n"
)

# Actual --format json output for a real finding.
_JSON_FINDING = json.dumps([
    {
        "filePath": "/app/file.js",
        "messages": [
            {
                "ruleId": "no-unused-vars",
                "severity": 2,
                "line": 5,
                "column": 7,
                "message": "'x' is defined but never used.",
                "nodeType": "Identifier",
                "endLine": 5,
                "endColumn": 8,
            },
        ],
        "errorCount": 1,
        "warningCount": 0,
        "fatalErrorCount": 0,
        "fixableErrorCount": 0,
        "fixableWarningCount": 0,
        "source": "const x = 1;\n",
        "usedDeprecatedRules": [],
    },
])

_JSON_CLEAN = "[]"


def _make_container(
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    """Create a mock container whose exec_run returns the given exit code/IO."""
    container = MagicMock()
    container.exec_run.return_value = (
        exit_code,
        (stdout.encode("utf-8"), stderr.encode("utf-8")),
    )
    return container


# ===================================================================
# _run_eslint_verify exit-code handling
# ===================================================================


class TestRunEslintVerifyExitCodes:
    """Direct tests for _run_eslint_verify exit code handling."""

    def test_exit_0_clean(self) -> None:
        """Exit 0 with empty JSON array -> status 'ok', no findings."""
        container = _make_container(0, _JSON_CLEAN)
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "ok"
        assert result.findings == []
        assert result.exit_code == 0

    def test_exit_1_with_findings(self) -> None:
        """Exit 1 with real JSON findings -> status 'findings', parsed rules."""
        container = _make_container(1, _JSON_FINDING)
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "findings"
        assert len(result.findings) == 1
        assert result.findings[0]["rule"] == "no-unused-vars"
        assert result.findings[0]["line"] == 5
        # severity should be assigned by _determine_lint_severity
        assert "severity" in result.findings[0]
        assert result.exit_code == 1

    def test_exit_2_no_config(self) -> None:
        """Exit 2 with empty stdout and ESLint config error stderr -> status
        'error', gate reports incomplete, stderr detail preserved."""
        container = _make_container(2, "", _STDERR_NO_CONFIG)
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "error", (
            f"Expected status='error' for exit 2 with empty stdout, "
            f"got status='{result.status}'"
        )
        assert result.findings == []
        assert result.exit_code == 2
        # The stderr text must survive so callers see why eslint failed.
        assert "ESLint couldn't find" in result.detail
        assert "config" in result.detail.lower()

    def test_exit_2_with_findings(self) -> None:
        """Exit 2 that still produces parseable JSON -> treat the data as
        findings, because stdout has real content.

        This is an unusual edge case (eslint may exit 2 for config issues
        *and* still emit findings before aborting).  Following the pyright
        pattern: when stdout parses to findings we trust the data.
        """
        container = _make_container(2, _JSON_FINDING)
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "findings"
        assert len(result.findings) == 1
        assert result.findings[0]["rule"] == "no-unused-vars"
        assert result.exit_code == 2

    def test_exit_127_not_available(self) -> None:
        """Exit 127 -> status 'not_available', tool absence."""
        container = _make_container(127)
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "not_available"
        assert "eslint" in result.detail.lower()

    def test_exit_3_generic_error(self) -> None:
        """Exit 3 (unexpected) with empty stdout -> status 'error'."""
        container = _make_container(3, "", "some internal error")
        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "error"
        assert "internal" in result.detail


# ===================================================================
# Edge cases: resolution annotation survives the new exit-code path
# ===================================================================


class TestRunEslintVerifyExit2Resolution:
    """Exit 2 must still carry the 'resolved via' annotation (Issue #588)."""

    def test_exit_2_local_resolution_annotated(self) -> None:
        """Local eslint that exits 2 still reports which binary ran."""
        container = MagicMock()
        # Two exec_run calls: (1) `test -x` for resolution, (2) eslint run
        container.exec_run.side_effect = [
            (0, (b"", b"")),          # local binary found
            (2, (b"", _STDERR_NO_CONFIG.encode("utf-8"))),  # eslint exit 2
        ]

        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "error"
        assert "resolved via local" in result.detail
        assert "eslint" in result.detail
        assert "ESLint couldn't find" in result.detail

    def test_exit_2_global_resolution_annotated(self) -> None:
        """Global eslint that exits 2 still reports which binary ran."""
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),          # no local binary
            (2, (b"", _STDERR_NO_CONFIG.encode("utf-8"))),  # eslint exit 2
        ]

        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "error"
        assert "resolved via global" in result.detail
        assert "ESLint couldn't find" in result.detail


# ===================================================================
# Gate-level consequence
# ===================================================================


class TestGateIncompleteOnEslintExit2:
    """The pre-test gate must report eslint-run-failure as incomplete."""

    def test_gate_reports_incomplete_but_passes(self) -> None:
        """run_lint_type_gate result shows incomplete=True and gate_passed=True."""
        from sunaba.edit_verify import gate as _gate

        with (
            patch.object(_gate, "detect_languages") as mock_detect,
            patch.object(_gate, "_run_eslint_verify") as mock_eslint,
        ):
            mock_detect.return_value = MagicMock(
                languages={"js"},
            )
            mock_eslint.return_value = VerifyResult(
                tool="eslint",
                status="error",
                detail=_STDERR_NO_CONFIG,
                exit_code=2,
            )

            gate_result = run_lint_type_gate(
                MagicMock(),
                scope=".",
                gate_on_lint=True,
                gate_on_type=False,
            )

            assert gate_result["gate_passed"] is True
            assert gate_result["incomplete"] is True
            assert gate_result["lint"] == []


class TestGateOkOnEslintExit1:
    """Normal lint findings still work through the gate."""

    def test_gate_fails_on_lint_findings(self) -> None:
        from sunaba.edit_verify import gate as _gate

        with (
            patch.object(_gate, "detect_languages") as mock_detect,
            patch.object(_gate, "_run_eslint_verify") as mock_eslint,
        ):
            mock_detect.return_value = MagicMock(
                languages={"js"},
            )

            finding = {
                "file": "file.js",
                "line": 5,
                "rule": "no-unused-vars",
                "message": "'x' is defined but never used",
                "severity": "warning",
            }
            mock_eslint.return_value = VerifyResult(
                tool="eslint",
                status="findings",
                findings=[finding],
                exit_code=1,
            )

            gate_result = run_lint_type_gate(
                MagicMock(),
                scope=".",
                gate_on_lint=True,
                gate_on_type=False,
            )

            assert gate_result["gate_passed"] is False
            assert gate_result["incomplete"] is False
            assert len(gate_result["lint"]) == 1


class TestGateOkOnEslintExit0:
    """Clean lint runs still pass cleanly through the gate."""

    def test_gate_passes_on_clean_lint(self) -> None:
        from sunaba.edit_verify import gate as _gate

        with (
            patch.object(_gate, "detect_languages") as mock_detect,
            patch.object(_gate, "_run_eslint_verify") as mock_eslint,
        ):
            mock_detect.return_value = MagicMock(
                languages={"js"},
            )
            mock_eslint.return_value = VerifyResult(
                tool="eslint",
                status="ok",
                findings=[],
                exit_code=0,
            )

            gate_result = run_lint_type_gate(
                MagicMock(),
                scope=".",
                gate_on_lint=True,
                gate_on_type=False,
            )

            assert gate_result["gate_passed"] is True
            assert gate_result["incomplete"] is False


# ===================================================================
# lint_in_container consequence: the reason must reach the caller
# ===================================================================


class TestLintFileEslintRunFailure:
    """``lint_file`` must not answer a config failure with "install eslint".

    ``_run_js_linter`` treats ``not_available`` and ``error`` alike, so
    routing a failed eslint run to ``error`` would otherwise turn "no
    config file" into "No JS/TS linter found in container. Install
    eslint" -- advice that sends the caller the wrong way, since eslint
    is installed (Issue #740).
    """

    @staticmethod
    def _client_for(container: MagicMock) -> MagicMock:
        client = MagicMock()
        client.containers.get.return_value = container
        return client

    def test_config_failure_reports_the_reason(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),  # no local binary -> global eslint
            (2, (b"", _STDERR_NO_CONFIG.encode("utf-8"))),
        ]

        findings = lint_file(self._client_for(container), "cid", "file.js")

        assert len(findings) == 1
        assert findings[0]["rule"] == "error"
        assert "ESLint couldn't find" in findings[0]["message"]
        assert "Install eslint" not in findings[0]["message"]

    def test_missing_eslint_still_says_install_eslint(self) -> None:
        container = _make_container(127, stderr="sh: eslint: not found\n")

        findings = lint_file(self._client_for(container), "cid", "file.js")

        assert len(findings) == 1
        assert findings[0]["rule"] == "no-linter"
        assert "Install eslint" in findings[0]["message"]

    def test_findings_path_is_untouched(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),  # no local binary -> global eslint
            (1, (_JSON_FINDING.encode("utf-8"), b"")),
        ]

        findings = lint_file(self._client_for(container), "cid", "file.js")

        assert len(findings) == 1
        assert findings[0]["rule"] == "no-unused-vars"
        assert findings[0]["line"] == 5
