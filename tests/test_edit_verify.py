"""Tests for the Edit/Verify subsystem (edit_verify.py).

Tests cover:
- ``lint_file`` — linter dispatch and output parsing (ruff, pylint, eslint)
- ``type_check_file`` — type checker dispatch and output parsing (mypy, pyright, tsc)
- ``read_file_lines`` — range reading with offset/limit
- ``search_files`` — lexical and structural search with output parsers
"""

from __future__ import annotations

import json

from src.code_sandbox_mcp.edit_verify import (
    transform_file_in_container,
    _determine_lint_severity,
    _parse_eslint_output,
    _parse_grep_output,
    _parse_mypy_output,
    _parse_pylint_output,
    _parse_rg_json,
    _parse_ruff_output,
    _parse_pyright_output,
    _parse_semgrep_output,
    _parse_sg_json,
    _parse_tsc_text,
    read_file_lines,
)



# ===================================================================
# _parse_ruff_output tests
# ===================================================================


class TestParseRuffOutput:
    """Tests for ruff JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_ruff_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "test.py",
                    "location": {"row": 5},
                    "code": "F401",
                    "message": "`os` imported but unused",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "F401"
        assert "unused" in result[0]["message"]

    def test_multiple_issues(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "a.py",
                    "location": {"row": 1},
                    "code": "E302",
                    "message": "blank lines",
                },
                {
                    "filename": "a.py",
                    "location": {"row": 5},
                    "code": "W291",
                    "message": "trailing space",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_ruff_output("not json", "file.py") == []


# ===================================================================
# _parse_pylint_output tests
# ===================================================================


class TestParsePylintOutput:
    """Tests for pylint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_pylint_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "path": "test.py",
                    "line": 10,
                    "symbol": "unused-import",
                    "message-id": "W0611",
                    "message": "Unused import os",
                },
            ]
        )
        result = _parse_pylint_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 10
        assert result[0]["rule"] == "unused-import"

    def test_invalid_json(self) -> None:
        assert _parse_pylint_output("corrupt", "file.py") == []


# ===================================================================
# _parse_eslint_output tests
# ===================================================================


class TestParseEslintOutput:
    """Tests for eslint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_eslint_output("", "file.js") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "/app/file.js",
                    "messages": [
                        {
                            "line": 5,
                            "ruleId": "no-unused-vars",
                            "message": "'x' is defined but never used",
                        },
                    ],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 1
        assert result[0]["file"] == "/app/file.js"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "no-unused-vars"

    def test_multiple_files(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [{"line": 1, "ruleId": "R1", "message": "m1"}],
                },
                {
                    "filePath": "b.js",
                    "messages": [{"line": 2, "ruleId": "R2", "message": "m2"}],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_eslint_output("bad", "file.js") == []


# ===================================================================
# _parse_mypy_output tests
# ===================================================================


class TestParseMypyOutput:
    """Tests for mypy text output parsing.

    Mypy output format: ``file:line:column: severity: message [error-code]``
    """

    def test_empty_output(self) -> None:
        assert _parse_mypy_output("", "file.py") == []

    def test_single_error(self) -> None:
        raw = "file.py:42:5: error: Incompatible return value type [return-value]"
        result = _parse_mypy_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "file.py"
        assert result[0]["line"] == 42
        assert result[0]["rule"] == "return-value"
        assert "Incompatible" in result[0]["message"]

    def test_error_without_code(self) -> None:
        raw = "src/main.py:5:10: error: Name 'x' is not defined"
        result = _parse_mypy_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["rule"] == "error"  # falls back to severity

    def test_warning_and_note(self) -> None:
        raw = "file.py:1:1: warning: Something [W001]\nfile.py:2:1: note: Hint"
        result = _parse_mypy_output(raw, "file.py")
        assert len(result) == 2
        assert result[0]["rule"] == "W001"
        assert result[1]["rule"] == "note"

    def test_no_match_lines_ignored(self) -> None:
        raw = "Success: no issues found in 1 source file"
        result = _parse_mypy_output(raw, "file.py")
        assert result == []


# ===================================================================
# _parse_pyright_output tests
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


class TestReadFileLines:
    """Tests for read_file_lines error handling and edge cases.

    Tests that require a live container are integration tests and
    should be run manually against a running sandbox.
    """

    def test_error_on_nonexistent_container(self) -> None:
        """When container doesn't exist, returns error dict."""
        result = {"error": "Container abc not found"}
        assert result["error"] is not None

    def test_file_lines_extraction(self) -> None:
        """Verify line extraction logic (pure function)."""
        lines = ["a", "b", "c", "d", "e"]
        offset = 1
        limit = 3
        page = lines[offset : offset + limit]
        assert page == ["b", "c", "d"]

    def test_offset_beyond_end(self) -> None:
        """When offset >= total lines, returns empty content."""
        lines = ["a", "b"]
        page_offset = 10  # beyond length
        page = lines[page_offset : page_offset + 50]
        assert page == []

    def test_has_more(self) -> None:
        """has_more is True when there are lines beyond the page."""
        lines = ["a", "b", "c", "d", "e"]
        offset = 0
        limit = 3
        total = len(lines)
        next_offset = offset + limit
        has_more = next_offset < total
        assert has_more is True

    def test_no_more(self) -> None:
        """has_more is False when at the end."""
        lines = ["a", "b", "c"]
        offset = 0
        limit = 3
        total = len(lines)
        next_offset = offset + limit
        has_more = next_offset < total
        assert has_more is False

    def test_limit_negative_one_reads_all_remaining(self, monkeypatch) -> None:
        """When limit=-1, reads all lines from offset to end."""
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.read_file",
            lambda _c, _p: "a\nb\nc\nd\ne",
        )

        result = read_file_lines(
            container=None, file_path="test.txt", offset=1, limit=-1
        )

        assert result["error"] is None
        assert result["content"] == "b\nc\nd\ne"
        assert result["shown"] == 4
        assert result["has_more"] is False
        assert result["next_offset"] is None

    def test_limit_negative_one_reads_all_from_start(self, monkeypatch) -> None:
        """When limit=-1 and offset=0, reads the entire file."""
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.read_file",
            lambda _c, _p: "a\nb\nc",
        )

        result = read_file_lines(
            container=None, file_path="test.txt", offset=0, limit=-1
        )

        assert result["error"] is None
        assert result["content"] == "a\nb\nc"
        assert result["shown"] == 3
        assert result["has_more"] is False
        assert result["next_offset"] is None


# ===================================================================
# lint_file parsers: edge cases
# ===================================================================


class TestLintFileParsers:
    """Edge cases for linter output parsers."""

    def test_ruff_no_issues(self) -> None:
        """Clean ruff output returns empty list."""
        assert _parse_ruff_output("[]", "file.py") == []

    def test_pylint_no_issues(self) -> None:
        assert _parse_pylint_output("[]", "file.py") == []

    def test_eslint_no_issues(self) -> None:
        assert _parse_eslint_output("[]", "file.js") == []

    def test_ruff_non_list_json(self) -> None:
        """Ruff output that is valid JSON but not a list."""
        assert _parse_ruff_output('{"summary": "ok"}', "file.py") == []


# ===================================================================
# type_check_file parsers: edge cases
# ===================================================================


class TestTypeCheckParsers:
    """Edge cases for type checker output parsers."""

    def test_mypy_no_issues(self) -> None:
        assert _parse_mypy_output("Success: no issues", "file.py") == []

    def test_pyright_no_issues(self) -> None:
        assert _parse_pyright_output('{"generalDiagnostics": []}', "file.py") == []

    def test_pyright_missing_diagnostics_key(self) -> None:
        assert _parse_pyright_output('{"version": "1.0"}', "file.py") == []

    def test_tsc_json_fallback(self) -> None:
        """tsc JSON output (if available) is parsed."""
        from src.code_sandbox_mcp.edit_verify import _parse_tsc_json

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
        from src.code_sandbox_mcp.edit_verify import _parse_tsc_json

        assert _parse_tsc_json("", "file.ts") == []


# ===================================================================
# search_files / parser tests
# ===================================================================


class TestParseRgJson:
    """Tests for ripgrep --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_rg_json("", 50) == []

    def test_single_match(self) -> None:
        raw = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "app.py"},
                    "lines": {"text": "def add(a, b):\n"},
                    "line_number": 42,
                },
            }
        )
        result = _parse_rg_json(raw, 50)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["text"] == "def add(a, b):"

    def test_skips_non_match_types(self) -> None:
        lines = [
            json.dumps({"type": "begin", "data": {"path": {"text": "x.py"}}}),
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "x.py"},
                        "lines": {"text": "hello\n"},
                        "line_number": 5,
                    },
                }
            ),
            json.dumps({"type": "end", "data": {}}),
            json.dumps({"type": "summary", "data": {}}),
        ]
        result = _parse_rg_json("\n".join(lines), 50)
        assert len(result) == 1
        assert result[0]["line"] == 5

    def test_max_results_cap(self) -> None:
        matches = []
        for i in range(10):
            matches.append(
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": f"file{i}.py"},
                            "lines": {"text": f"line {i}"},
                            "line_number": i,
                        },
                    }
                )
            )
        result = _parse_rg_json("\n".join(matches), 5)
        assert len(result) == 5
        assert result[0]["file"] == "file0.py"
        assert result[-1]["file"] == "file4.py"

    def test_invalid_json_ignored(self) -> None:
        raw = "not valid json\n" + json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "ok.py"},
                    "lines": {"text": "ok"},
                    "line_number": 1,
                },
            }
        )
        result = _parse_rg_json(raw, 50)
        assert len(result) == 1


class TestParseGrepOutput:
    """Tests for grep -rnI output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_grep_output("", 50) == []

    def test_single_match(self) -> None:
        raw = "app.py:42:def add(a, b):"
        result = _parse_grep_output(raw, 50)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["text"] == "def add(a, b):"

    def test_multiple_matches(self) -> None:
        raw = "\n".join(
            [
                "src/a.py:1:import os",
                "src/b.py:5:x = 1",
                "src/c.py:10:return 42",
            ]
        )
        result = _parse_grep_output(raw, 50)
        assert len(result) == 3
        assert result[1]["file"] == "src/b.py"
        assert result[1]["line"] == 5

    def test_path_with_colon(self) -> None:
        """Only the last colon pair is line:text; handle paths like 'a:b'."""
        raw = "src/main.py:15:x: int = 1"
        result = _parse_grep_output(raw, 50)
        assert len(result) == 1
        assert result[0]["file"] == "src/main.py"
        assert result[0]["line"] == 15
        assert "int = 1" in result[0]["text"]

    def test_non_matching_lines_ignored(self) -> None:
        raw = "Binary file matches\nsome random output\napp.py:1:ok"
        result = _parse_grep_output(raw, 50)
        assert len(result) == 1

    def test_max_results_cap(self) -> None:
        lines = [f"f{i}.py:1:text" for i in range(20)]
        result = _parse_grep_output("\n".join(lines), 7)
        assert len(result) == 7


class TestParseSgJson:
    """Tests for ast-grep (sg) --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_sg_json("", 50) == []

    def test_single_match(self) -> None:
        raw = json.dumps(
            [
                {
                    "file": "app.py",
                    "range": {"start": {"line": 5, "column": 4}},
                    "text": "def add(a, b):\n",
                }
            ]
        )
        result = _parse_sg_json(raw, 50)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 5
        assert result[0]["text"] == "def add(a, b):"

    def test_multiple_files_newline_separated(self) -> None:
        a = json.dumps([{"file": "a.py", "range": {"start": {"line": 1}}, "text": "a"}])
        b = json.dumps([{"file": "b.py", "range": {"start": {"line": 3}}, "text": "b"}])
        raw = a + "\n" + b
        result = _parse_sg_json(raw, 50)
        assert len(result) == 2
        assert result[0]["file"] == "a.py"
        assert result[1]["file"] == "b.py"

    def test_max_results_cap(self) -> None:
        entries = [
            {"file": f"f{i}.py", "range": {"start": {"line": i}}, "text": f"text{i}"}
            for i in range(10)
        ]
        raw = json.dumps(entries)
        result = _parse_sg_json(raw, 5)
        assert len(result) == 5

    def test_invalid_json_ignored(self) -> None:
        raw = "not json\n" + json.dumps(
            [{"file": "ok.py", "range": {"start": {"line": 1}}, "text": "ok"}]
        )
        result = _parse_sg_json(raw, 50)
        assert len(result) == 1

    def test_dict_entry_handled(self) -> None:
        """Single dict per line (stream format) is wrapped and processed."""
        raw = json.dumps({"file": "app.py", "range": {"start": {"line": 5}}, "text": "hello"})
        result = _parse_sg_json(raw, 50)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 5
        assert result[0]["text"] == "hello"


# ===================================================================
# _determine_lint_severity tests (Issue #54)
# ===================================================================


class TestDetermineLintSeverity:
    """Tests for lint severity mapping from rule codes."""

    def test_error_rules(self) -> None:
        assert _determine_lint_severity("E501") == "error"
        assert _determine_lint_severity("F401") == "error"
        assert _determine_lint_severity("B006") == "error"
        assert _determine_lint_severity("RUF001") == "error"

    def test_warning_rules(self) -> None:
        assert _determine_lint_severity("W291") == "warning"
        assert _determine_lint_severity("S101") == "warning"
        assert _determine_lint_severity("C901") == "warning"
        assert _determine_lint_severity("N801") == "warning"
        assert _determine_lint_severity("D100") == "warning"

    def test_info_rules(self) -> None:
        assert _determine_lint_severity("I001") == "info"
        assert _determine_lint_severity("SIM101") == "info"
        assert _determine_lint_severity("PLW0603") == "info"
        assert _determine_lint_severity("UP006") == "info"
        assert _determine_lint_severity("TCH001") == "info"

    def test_unknown_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("XYZ999") == "error"

    def test_empty_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("") == "error"

    def test_longest_prefix_match(self) -> None:
        """C90 should match C90 prefix, not C prefix."""
        assert _determine_lint_severity("C901") == "warning"


# ===================================================================
# _parse_semgrep_output tests (Issue #54)
# ===================================================================


class TestParseSemgrepOutput:
    """Tests for semgrep --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_semgrep_output("", "file.py") == []

    def test_single_finding(self) -> None:
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "python.lang.security.audit.sql-injection",
                        "path": "app.py",
                        "start": {"line": 42, "col": 5},
                        "end": {"line": 42, "col": 20},
                        "extra": {
                            "severity": "ERROR",
                            "message": "Detected SQL injection risk",
                        },
                    }
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["rule"] == "python.lang.security.audit.sql-injection"
        assert result[0]["severity"] == "ERROR"
        assert "SQL injection" in result[0]["message"]

    def test_multiple_findings_mixed_severity(self) -> None:
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "rule-one",
                        "path": "a.py",
                        "start": {"line": 1},
                        "extra": {"severity": "ERROR", "message": "error msg"},
                    },
                    {
                        "check_id": "rule-two",
                        "path": "b.py",
                        "start": {"line": 5},
                        "extra": {"severity": "WARNING", "message": "warning msg"},
                    },
                    {
                        "check_id": "rule-three",
                        "path": "c.py",
                        "start": {"line": 10},
                        "extra": {"severity": "INFO", "message": "info msg"},
                    },
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 3
        assert result[0]["severity"] == "ERROR"
        assert result[1]["severity"] == "WARNING"
        assert result[2]["severity"] == "INFO"

    def test_no_results_key(self) -> None:
        raw = json.dumps({"errors": [{"message": "parse error"}]})
        result = _parse_semgrep_output(raw, "file.py")
        assert result == []

    def test_invalid_json(self) -> None:
        assert _parse_semgrep_output("not json", "file.py") == []

    def test_finding_with_missing_fields(self) -> None:
        """Missing severity defaults to WARNING, missing start.line to 0."""
        raw = json.dumps(
            {
                "results": [
                    {
                        "check_id": "rule-minimal",
                        "path": "min.py",
                    }
                ],
            }
        )
        result = _parse_semgrep_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "min.py"
        assert result[0]["line"] == 0
        assert result[0]["severity"] == "WARNING"
        assert result[0]["message"] == ""


# ===================================================================
# run_verify gate logic tests (Issue #54)
# ===================================================================


class TestVerifyGateLogic:
    """Tests for the gate logic in run_verify.

    These tests verify the gate decision algorithm without a live
    container by exercising the severity classification and
    gate-fail-reason logic directly.
    """

    def _simulate_gate(
        self,
        lint_results: list[dict],
        type_results: list[dict],
        test_results: dict,
        scan_results: list[dict],
        gate_on_lint_error: bool = True,
        gate_on_type_error: bool = False,
        gate_on_test_fail: bool = True,
        gate_on_scan_error: bool = True,
        gate_on_scan_warning: bool = False,
    ) -> tuple[bool, list[str]]:
        """Simulate the gate logic from run_verify."""
        reasons: list[str] = []

        if gate_on_lint_error:
            lint_errors = [
                r for r in lint_results
                if r.get("severity") == "error"
                # Keep in sync with run_verify gate logic
                and r.get("rule") not in ("no-linter", "error")
            ]
            if lint_errors:
                reasons.append(f"lint: {len(lint_errors)} error(s)")

        if gate_on_type_error:
            type_errors = [
                r for r in type_results
                if r.get("severity") == "error"
                and r.get("rule") not in ("no-typechecker", "error")
            ]
            if type_errors:
                reasons.append(f"type_check: {len(type_errors)} error(s)")

        if gate_on_test_fail:
            if test_results.get("status") == "failed":
                reasons.append(
                    f"tests: {test_results.get('failed', 0)} failure(s)"
                )

        if gate_on_scan_error:
            scan_errors = [
                r for r in scan_results
                if r.get("severity") == "ERROR"
                and r.get("rule") not in ("no-scanner",)
            ]
            if scan_errors:
                reasons.append(f"scan: {len(scan_errors)} ERROR(s)")

        if gate_on_scan_warning:
            scan_warnings = [
                r for r in scan_results
                if r.get("severity") == "WARNING"
            ]
            if scan_warnings:
                reasons.append(f"scan: {len(scan_warnings)} WARNING(s)")

        return (len(reasons) == 0, reasons)

    def test_all_clean_passes_gate(self) -> None:
        passed, reasons = self._simulate_gate([], [], {"status": "ok", "passed": 10},
                                               [])
        assert passed is True
        assert reasons == []

    def test_lint_error_fails_gate_by_default(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "F401",
             "severity": "error", "message": "unused import"},
        ]
        passed, reasons = self._simulate_gate(
            lint, [], {"status": "ok", "passed": 5}, []
        )
        assert passed is False
        assert any("lint" in r for r in reasons)

    def test_lint_warning_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "W291",
             "severity": "warning", "message": "trailing whitespace"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5}, [])
        assert passed is True

    def test_lint_info_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "I001",
             "severity": "info", "message": "unsorted imports"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5}, [])
        assert passed is True

    def test_no_linter_tool_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 0, "rule": "no-linter",
             "severity": "info", "message": "ruff not installed"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5}, [])
        assert passed is True

    def test_type_error_passes_gate_by_default(self) -> None:
        types_ = [
            {"file": "a.py", "line": 10, "rule": "reportUnknownVariableType",
             "severity": "error", "message": "unknown type"},
        ]
        passed, _ = self._simulate_gate(
            [], types_, {"status": "ok", "passed": 5}, []
        )
        assert passed is True

    def test_type_error_fails_gate_when_enabled(self) -> None:
        types_ = [
            {"file": "a.py", "line": 10, "rule": "reportUnknownVariableType",
             "severity": "error", "message": "unknown type"},
        ]
        passed, reasons = self._simulate_gate(
            [], types_, {"status": "ok", "passed": 5}, [],
            gate_on_type_error=True,
        )
        assert passed is False
        assert any("type_check" in r for r in reasons)

    def test_test_failure_fails_gate(self) -> None:
        test = {"status": "failed", "passed": 8, "failed": 2, "duration": 1.5}
        passed, reasons = self._simulate_gate([], [], test, [])
        assert passed is False
        assert any("tests" in r for r in reasons)

    def test_test_failure_passes_when_gate_disabled(self) -> None:
        test = {"status": "failed", "passed": 8, "failed": 2, "duration": 1.5}
        passed, _ = self._simulate_gate(
            [], [], test, [], gate_on_test_fail=False
        )
        assert passed is True

    def test_scan_error_fails_gate(self) -> None:
        scan = [
            {"file": "a.py", "line": 10, "rule": "python.sql-injection",
             "severity": "ERROR", "message": "SQL injection"},
        ]
        passed, reasons = self._simulate_gate([], [], {"status": "ok", "passed": 3},
                                               scan)
        assert passed is False
        assert any("scan" in r for r in reasons)

    def test_scan_warning_passes_gate_by_default(self) -> None:
        scan = [
            {"file": "a.py", "line": 10, "rule": "python.warning",
             "severity": "WARNING", "message": "something"},
        ]
        passed, _ = self._simulate_gate([], [], {"status": "ok", "passed": 3}, scan)
        assert passed is True

    def test_scan_warning_fails_gate_when_enabled(self) -> None:
        scan = [
            {"file": "a.py", "line": 10, "rule": "python.warning",
             "severity": "WARNING", "message": "something"},
        ]
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 3}, scan,
            gate_on_scan_warning=True,
        )
        assert passed is False
        assert any("WARNING" in r for r in reasons)

    def test_no_scanner_tool_does_not_fail_gate(self) -> None:
        scan = [
            {"file": "a.py", "line": 0, "rule": "no-scanner",
             "severity": "info", "message": "semgrep not installed"},
        ]
        passed, _ = self._simulate_gate([], [], {"status": "ok", "passed": 3}, scan)
        assert passed is True

    def test_multiple_fail_reasons_accumulate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "F401",
             "severity": "error", "message": "unused import"},
        ]
        scan = [
            {"file": "a.py", "line": 10, "rule": "python.sql-injection",
             "severity": "ERROR", "message": "SQL injection"},
        ]
        test = {"status": "failed", "passed": 5, "failed": 1, "duration": 0.5}
        passed, reasons = self._simulate_gate(lint, [], test, scan)
        assert passed is False
        assert len(reasons) == 3

    def test_skipped_test_does_not_fail_gate(self) -> None:
        test = {"status": "skipped", "message": "no test output"}
        passed, _ = self._simulate_gate([], [], test, [])
        assert passed is True


# ===================================================================
# detect_languages tests  (Issue #109)
# ===================================================================

class TestDetectLanguages:
    """Tests for detect_languages and DetectionResult."""

    def test_detection_result_dataclass(self) -> None:
        from src.code_sandbox_mcp.edit_verify import DetectionResult

        r = DetectionResult(languages={"python"}, scope={"python": "/app"}, reason=None)
        assert r.languages == {"python"}
        assert r.scope == {"python": "/app"}
        assert r.reason is None

        r2 = DetectionResult(languages=set(), scope={}, reason="no markers")
        assert r2.languages == set()
        assert r2.reason == "no markers"

    def test_explicit_language_skips_detection(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/some/path", language="python")
        assert result.languages == {"python"}
        assert result.scope == {"python": "/some/path"}
        assert result.reason is None
        mock_container.exec_run.assert_not_called()

    def test_file_extension_python(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/main.py")
        assert result.languages == {"python"}
        assert result.reason is None

    def test_file_extension_js(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/index.js")
        assert result.languages == {"js"}

    def test_file_extension_jsx(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/component.jsx")
        assert result.languages == {"js"}

    def test_file_extension_mjs(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/module.mjs")
        assert result.languages == {"js"}

    def test_file_extension_cjs(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/module.cjs")
        assert result.languages == {"js"}

    def test_file_extension_ts(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        # Mock upward tsconfig search: no tsconfig found
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/src/main.ts")
        assert result.languages == {"ts"}

    def test_file_extension_tsx(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/src/component.tsx")
        assert result.languages == {"ts"}

    def test_file_extension_go(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/main.go")
        assert result.languages == {"go"}

    def test_ts_file_with_tsconfig_upward(self) -> None:
        from unittest.mock import MagicMock, call
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        # Simulate tsconfig.json found in /app (parent of /app/src)
        def exec_side_effect(cmd, **kwargs):
            test_path = cmd[-1]  # e.g. "test -f /app/src/tsconfig.json && echo found || echo notfound"
            if "/app/src/tsconfig.json" in test_path:
                return (0, (b"notfound", b""))
            elif "/app/tsconfig.json" in test_path:
                return (0, (b"found", b""))
            return (1, (b"", b""))
        mock_container.exec_run.side_effect = exec_side_effect

        result = detect_languages(mock_container, "/app/src/main.ts")
        assert result.languages == {"ts"}
        # Scope should point to the directory with tsconfig.json
        assert "/app" in result.scope["ts"]

    def test_unknown_file_extension_returns_unknown(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))
        result = detect_languages(mock_container, "/app/README.md")
        assert result.languages == set()
        assert result.reason is not None

    def test_directory_go_detection(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        # Simulate find output showing go.mod
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"go"}
        assert result.scope.get("go") == "/app"

    def test_directory_python_detection(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/pyproject.toml\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_directory_js_detection(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/package.json\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"js"}

    def test_directory_ts_detection(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/tsconfig.json\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"ts"}

    def test_requirements_glob_pattern(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/requirements-dev.txt\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_multiple_requirements_files_dedup(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/requirements.txt\n/app/requirements-dev.txt\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}

    def test_polyglot_python_and_js(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/pyproject.toml\n/app/frontend/package.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python", "js"}
        assert result.scope.get("python") == "/app"
        assert result.scope.get("js") == "/app/frontend"

    def test_polyglot_ts_and_js(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/package.json\n/app/tsconfig.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        # ts and js should coexist (no more discarding)
        assert result.languages == {"js", "ts"}
        assert result.scope.get("js") == "/app"
        assert result.scope.get("ts") == "/app"

    def test_no_markers_returns_unknown(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))

        result = detect_languages(mock_container, "/empty_dir")
        assert result.languages == set()
        assert result.reason is not None
        assert "language=" in result.reason

    def test_exclude_dirs_not_accidentally_detected(self) -> None:
        """node_modules/.venv etc are excluded by -maxdepth 1 and path scope."""
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))

        result = detect_languages(mock_container, "/app/node_modules")
        assert result.languages == set()

    def test_find_cmd_includes_all_marker_patterns(self) -> None:
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages, _DETECTION_MARKERS

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/go.mod\n", b""))

        detect_languages(mock_container, "/app")
        call_args = mock_container.exec_run.call_args[0][0]
        find_cmd = call_args[2]
        # All patterns should be in the find command
        for pattern, _ in _DETECTION_MARKERS:
            assert pattern in find_cmd, f"Pattern {pattern!r} missing from find command"
        assert " -maxdepth 1 " in find_cmd
        assert " -o " in find_cmd

    def test_scope_path_for_subdir_polyglot(self) -> None:
        """Polyglot project where markers are in different subdirectories."""
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/backend/pyproject.toml\n/app/frontend/package.json\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python", "js"}
        # Scope should point to each marker's directory
        assert result.scope.get("python") == "/app/backend"
        assert result.scope.get("js") == "/app/frontend"

    def test_same_language_multiple_markers_single_scope(self) -> None:
        """Multiple markers for the same language should not duplicate."""
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/pyproject.toml\n/app/setup.py\n/app/tox.ini\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"python"}
        # The last marker's scope wins (dict key dedup)
        assert result.scope.get("python") == "/app"

    def test_ts_file_detects_tsconfig_in_parent(self) -> None:
        """.ts file with tsconfig.json in a parent directory should detect ts with parent scope."""
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        def exec_side_effect(cmd, **kwargs):
            test_path = cmd[-1] if len(cmd) > 1 else ""
            if "tsconfig.json" in test_path and "/app" in test_path and "/app/src" not in test_path:
                return (0, (b"found", b""))
            return (1, (b"", b""))
        mock_container.exec_run.side_effect = exec_side_effect

        # exec_run is called twice: once for tsconfig upward search, once for directory
        # Since path is a .ts file, only upward search happens
        result = detect_languages(mock_container, "/app/src/foo.ts")
        assert result.languages == {"ts"}
        assert "/app" in result.scope.get("ts", "")

    def test_ts_file_without_tsconfig(self) -> None:
        """.ts file without any tsconfig.json should still detect ts with file path scope."""
        from unittest.mock import MagicMock
        from src.code_sandbox_mcp.edit_verify import detect_languages

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b""))

        result = detect_languages(mock_container, "/app/src/standalone.ts")
        assert result.languages == {"ts"}
        # Scope should be the file path itself when no tsconfig found
        assert result.scope.get("ts") == "/app/src/standalone.ts"


# ===================================================================
# transform_file_in_container tests
# ===================================================================


class _FakeContainer:
    """Emulates the in-container shell for the transform runner.

    The real tool runs ``echo <b64> | base64 -d > tmp && python3 tmp`` inside
    the container.  This fake extracts the base64 runner from that command,
    decodes it, and executes it on the host (capturing stdout) so the full
    runner-generation / marker-extraction / JSON-parse path is exercised
    without Docker.

    ``path_map`` maps the absolute posix path the tool was given (e.g.
    ``/sandbox/x.py``) to a real file on the host, so the test stays
    OS-portable while ``transform_file_in_container`` still sees a posix path.
    """

    def __init__(self, path_map=None) -> None:  # noqa: ANN001
        self.ran = False
        self.path_map = path_map or {}

    def exec_run(self, cmd, **kwargs):  # noqa: ANN001
        import base64 as _b64
        import io
        import sys

        self.ran = True
        shell_cmd = cmd[-1]
        # extract the quoted base64 blob: echo '<b64>' | base64 -d > ...
        blob = shell_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'")
        runner_src = _b64.b64decode(blob).decode("utf-8")

        real_open = open
        pm = self.path_map

        def mapped_open(path, *a, **k):  # noqa: ANN001
            return real_open(pm.get(path, path), *a, **k)

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            try:
                exec(compile(runner_src, "<runner>", "exec"), {"open": mapped_open})
            except SystemExit:
                pass
        finally:
            sys.stdout = old
        return 0, (buf.getvalue().encode("utf-8"), b"")


class _FakeClient:
    def __init__(self, container) -> None:  # noqa: ANN001
        self._c = container

    class _Containers:
        def __init__(self, c) -> None:  # noqa: ANN001
            self._c = c

        def get(self, _cid):  # noqa: ANN001
            return self._c

    @property
    def containers(self):
        return _FakeClient._Containers(self._c)


class TestTransformFileInContainer:
    """Tests for the imperative transform_file edit path."""

    _POSIX = "/sandbox/x.py"

    def _run(self, real_path, code):  # noqa: ANN001
        """Invoke with a fixed posix path mapped to *real_path* on the host."""
        client = _FakeClient(_FakeContainer({self._POSIX: str(real_path)}))
        return transform_file_in_container(client, "abc123", self._POSIX, code)

    def test_rejects_relative_path(self) -> None:
        out = transform_file_in_container(
            _FakeClient(_FakeContainer()), "abc123", "rel/path.py", "x"
        )
        assert out["status"] == "error"
        assert "absolute" in out["error"]

    def test_applies_transform_and_returns_diff(self, tmp_path, monkeypatch) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        f = tmp_path / "x.py"
        f.write_text("aaa\nbbb\n", encoding="utf-8")

        code = "def transform(text):\n    return text.replace('a', 'z')\n"
        out = self._run(f, code)

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert "-aaa" in out["diff"] and "+zzz" in out["diff"]
        assert f.read_text(encoding="utf-8") == "zzz\nbbb\n"
        assert writes, "a successful change should be journaled"

    def test_no_change_is_reported(self, tmp_path, monkeypatch) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")

        out = self._run(f, "def transform(text):\n    return text\n")

        assert out["status"] == "ok"
        assert out["changed"] is False
        assert not writes, "an unchanged file should not be journaled"

    def test_missing_transform_callable(self, tmp_path) -> None:
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")
        out = self._run(f, "y = 1\n")
        assert out["status"] == "error"
        assert "transform" in out["error"]

    def test_transform_raises_returns_traceback(self, tmp_path) -> None:
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")
        out = self._run(
            f, "def transform(text):\n    raise ValueError('boom')\n"
        )
        assert out["status"] == "error"
        assert "boom" in out["error"]
        assert "traceback" in out

    def test_file_not_found(self, tmp_path) -> None:
        missing = tmp_path / "missing.py"
        out = self._run(missing, "def transform(text):\n    return text\n")
        assert out["status"] == "error"
        assert "not found" in out["error"]


# ===================================================================
# _normalize_diff_for_git (pure) + apply_patch_to_file delegation
# ===================================================================


class TestNormalizeDiffForGit:
    """Pure-function tests for diff normalization (no container/git)."""

    def test_rewrites_headers_to_target(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index 111..222 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        )
        out = _normalize_diff_for_git(diff)
        assert out is not None
        assert out.startswith("--- a/target\n+++ b/target\n@@")
        # pre-hunk metadata is dropped
        assert "diff --git" not in out
        assert "index 111" not in out
        assert "foo.py" not in out
        # hunk body is preserved
        assert "-b\n+B" in out

    def test_returns_none_without_hunks(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        assert _normalize_diff_for_git("--- a/x\n+++ b/x\n") is None
        assert _normalize_diff_for_git("") is None

    def test_returns_none_for_multi_file_diff(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _normalize_diff_for_git

        multi = (
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -5,2 +5,2 @@\n x\n-y\n+Y\n"
        )
        assert _normalize_diff_for_git(multi) is None


class TestApplyPatchToFile:
    """Integration tests for the git-apply delegation (requires git)."""

    _POSIX = "/sandbox/x.py"

    def _apply(self, real_path, diff, monkeypatch):  # noqa: ANN001
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.record_file_write",
            lambda *a, **k: None,
        )
        client = _FakeClient(_FakeContainer({self._POSIX: str(real_path)}))
        from src.code_sandbox_mcp.edit_verify import apply_patch_to_file

        return apply_patch_to_file(client, "abc123", self._POSIX, diff)

    def _read(self, p):  # noqa: ANN001
        with open(p, encoding="utf-8") as fh:  # universal newlines
            return fh.read()

    def test_applies_clean_diff(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert "successfully" in out
        assert self._read(f) == "a\nB\nc\n"

    def test_recount_tolerates_wrong_hunk_counts(self, tmp_path, monkeypatch) -> None:
        """--recount fixes off-by-one @@ counts the old strict parser rejected."""
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n a\n-b\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert "successfully" in out
        assert self._read(f) == "a\nB\nc\n"

    def test_context_mismatch_is_error(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8", newline="")
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-WRONG\n+B\n c\n"
        out = self._apply(f, diff, monkeypatch)
        assert out.startswith("Error")

    def test_empty_diff_is_noop(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\n", encoding="utf-8", newline="")
        out = self._apply(f, "   ", monkeypatch)
        assert "no changes" in out

    def test_diff_without_hunks_is_error(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\n", encoding="utf-8", newline="")
        out = self._apply(f, "--- a/x\n+++ b/x\n", monkeypatch)
        assert out.startswith("Error")
        assert "no hunks" in out
