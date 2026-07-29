"""Tests for type checker output parsers (pyright, tsc)."""

from __future__ import annotations

import json

from src.sunaba.edit_verify import (
    _parse_pyright_output,
    _parse_tsc_text,
)

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestParsePyrightOutput:
    """Tests for pyright JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_pyright_output("", "file.py") == []

    def test_single_diagnostic(self) -> None:
        raw = json.dumps(
            {
                "generalDiagnostics": [
                    {
                        "file": "test.py",
                        "range": {"start": {"line": 10}},
                        "rule": "reportUnknownVariableType",
                        "message": "Type of 'x' is unknown",
                    },
                ],
            }
        )
        result = _parse_pyright_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 11  # 0-indexed → 1-indexed
        assert result[0]["rule"] == "reportUnknownVariableType"

    def test_invalid_json(self) -> None:
        assert _parse_pyright_output("{bad", "file.py") == []

    def test_preserves_severity_warning(self) -> None:
        """Pyright's severity 'warning' must be preserved (Issue #747)."""
        raw = json.dumps(
            {
                "generalDiagnostics": [
                    {
                        "file": "test.py",
                        "range": {"start": {"line": 3}},
                        "rule": "reportMissingImports",
                        "message": "Cannot resolve import",
                        "severity": "warning",
                    },
                ],
            }
        )
        result = _parse_pyright_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["severity"] == "warning"

    def test_preserves_severity_error(self) -> None:
        """Pyright's severity 'error' must be preserved."""
        raw = json.dumps(
            {
                "generalDiagnostics": [
                    {
                        "file": "test.py",
                        "range": {"start": {"line": 5}},
                        "rule": "reportGeneralTypeIssues",
                        "message": "Type mismatch",
                        "severity": "error",
                    },
                ],
            }
        )
        result = _parse_pyright_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["severity"] == "error"

    def test_defaults_to_error_when_severity_missing(self) -> None:
        """A pyright diagnostic without severity should default to 'error'
        for backward compatibility (Issue #747)."""
        raw = json.dumps(
            {
                "generalDiagnostics": [
                    {
                        "file": "test.py",
                        "range": {"start": {"line": 7}},
                        "rule": "reportUnknownRule",
                        "message": "Something",
                    },
                ],
            }
        )
        result = _parse_pyright_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["severity"] == "error"


# ===================================================================
# _parse_tsc_text tests
# ===================================================================


# ===================================================================
# _parse_tsc_text tests
# ===================================================================




class TestParseTscText:
    """Tests for tsc text output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_tsc_text("", "file.ts") == []

    def test_single_error(self) -> None:
        raw = "src/app.ts(42,5): error TS2322: Type 'A' is not assignable to type 'B'"
        result = _parse_tsc_text(raw, "file.ts")
        assert len(result) == 1
        assert result[0]["file"] == "src/app.ts"
        assert result[0]["line"] == 42
        assert result[0]["rule"] == "TS2322"

    def test_multiple_errors(self) -> None:
        raw = (
            "a.ts(1,1): error TS1000: First error\n"
            "b.ts(2,5): warning TS2000: Second issue\n"
        )
        result = _parse_tsc_text(raw, "file.ts")
        assert len(result) == 2

    def test_no_match_ignored(self) -> None:
        raw = "Some random output that doesn't match tsc format"
        result = _parse_tsc_text(raw, "file.ts")
        assert result == []


# ===================================================================
# read_file_lines tests (logic only, no container)
# ===================================================================




class TestTypeCheckParsers:
    """Edge cases for type checker output parsers."""


    def test_pyright_no_issues(self) -> None:
        assert _parse_pyright_output('{"generalDiagnostics": []}', "file.py") == []

    def test_pyright_missing_diagnostics_key(self) -> None:
        assert _parse_pyright_output('{"version": "1.0"}', "file.py") == []

    def test_tsc_json_fallback(self) -> None:
        """tsc JSON output (if available) is parsed."""
        from src.sunaba.edit_verify import _parse_tsc_json

        raw = json.dumps(
            {
                "diagnostics": [
                    {
                        "file": {"fileName": "test.ts", "line": 5},
                        "code": "TS1234",
                        "messageText": "Some error",
                    },
                ],
            }
        )
        result = _parse_tsc_json(raw, "file.ts")
        assert len(result) == 1
        assert result[0]["file"] == "test.ts"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "TS1234"

    def test_tsc_json_empty(self) -> None:
        from src.sunaba.edit_verify import _parse_tsc_json

        assert _parse_tsc_json("", "file.ts") == []


# ===================================================================
# search_files / parser tests
# ===================================================================


class TestNeedsPCRE2:
    """Tests for _needs_pcre2 helper."""

    def test_lookahead_positive(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert _needs_pcre2(r"foo(?=bar)")

    def test_lookahead_negative(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert _needs_pcre2(r"foo(?!bar)")

    def test_lookbehind_positive(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert _needs_pcre2(r"(?<=foo)bar")

    def test_lookbehind_negative(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert _needs_pcre2(r"(?<!foo)bar")

    def test_non_capturing_group_not_detected(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2(r"(?:foo)")

    def test_flags_not_detected(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2(r"(?i)foo")

    def test_named_group_not_detected(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2(r"(?P<name>foo)")

    def test_simple_pattern_not_detected(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2(r"foo.*bar")

    def test_atomic_group_not_detected(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2(r"(?>foo)")

    def test_empty_pattern(self) -> None:
        from src.sunaba.search import _needs_pcre2
        assert not _needs_pcre2("")


class TestRunPyrightVerify:
    """Tests for _run_pyright_verify with mocked container."""

    def _make_container(self, ec: int, stdout: str = "", stderr: str = ""):
        """Create a mock container with docker-py compliant exec_run."""
        from tests.conftest import _make_docker_compliant_container

        return _make_docker_compliant_container([
            (ec, stdout.encode("utf-8"), stderr.encode("utf-8")),
        ])

    def test_exit_code_1_returns_findings(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        pyright_output = json.dumps({
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "severity": "error",
                    "message": "Cannot assign to None",
                    "range": {"start": {"line": 5}},
                    "rule": "reportGeneralTypeIssues",
                }
            ]
        })
        container = self._make_container(1, pyright_output)

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "findings"
        assert len(result.findings) == 1
        assert result.findings[0]["line"] == 6

    def test_exit_code_0_returns_ok(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        pyright_output = json.dumps({"generalDiagnostics": []})
        container = self._make_container(0, pyright_output)

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "ok"
        assert result.findings == []

    def test_exit_code_127_returns_not_available(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        container = self._make_container(127)

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "not_available"

    def test_exit_code_250_with_findings_returns_ok(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        pyright_output = json.dumps({
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "severity": "error",
                    "message": "Undefined variable",
                    "range": {"start": {"line": 10}},
                    "rule": "reportUndefinedVariable",
                }
            ]
        })
        container = self._make_container(250, pyright_output)

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "findings"
        assert len(result.findings) == 1

    def test_exit_code_250_without_findings_returns_error(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        container = self._make_container(250, "", "FATAL ERROR: OOM")

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "error"

    def test_exit_code_2_with_empty_output_returns_error(self) -> None:
        from sunaba.edit_verify import _run_pyright_verify

        container = self._make_container(2)

        result = _run_pyright_verify(container, "/app/test.py")
        assert result.status == "error"

    def test_warning_severity_preserved_by_runner(self) -> None:
        """_run_pyright_verify must not overwrite pyright's own severity
        (Issue #747).  A 'warning' finding should stay 'warning'."""
        from sunaba.edit_verify import _run_pyright_verify

        pyright_output = json.dumps({
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "severity": "warning",
                    "message": "Cannot resolve import",
                    "range": {"start": {"line": 3}},
                    "rule": "reportMissingImports",
                }
            ]
        })
        container = self._make_container(1, pyright_output)

        result = _run_pyright_verify(container, "/workspace/test.py")
        assert result.status == "findings"
        assert len(result.findings) == 1
        assert result.findings[0]["severity"] == "warning"

    def test_error_severity_preserved_by_runner(self) -> None:
        """An 'error' finding from pyright must stay 'error' (Issue #747)."""
        from sunaba.edit_verify import _run_pyright_verify

        pyright_output = json.dumps({
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "severity": "error",
                    "message": "Type mismatch",
                    "range": {"start": {"line": 10}},
                    "rule": "reportGeneralTypeIssues",
                }
            ]
        })
        container = self._make_container(1, pyright_output)

        result = _run_pyright_verify(container, "/workspace/test.py")
        assert result.status == "findings"
        assert len(result.findings) == 1
        assert result.findings[0]["severity"] == "error"


# ===================================================================
# detect_languages tests  (Issue #109)
# ===================================================================


# ===================================================================
# detect_languages tests  (Issue #109)
# ===================================================================
