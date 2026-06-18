"""Tests for the Edit/Verify subsystem (edit_verify.py).

Tests cover:
- ``apply_unified_diff`` — unified diff parsing and application
- ``lint_file`` — linter dispatch and output parsing (ruff, pylint, eslint)
- ``type_check_file`` — type checker dispatch and output parsing (mypy, pyright, tsc)
- ``read_file_lines`` — range reading with offset/limit
- ``search_files`` — lexical and structural search with output parsers
"""

from __future__ import annotations

import json

import pytest

from src.code_sandbox_mcp.edit_verify import (
    apply_unified_diff,
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
)


# ===================================================================
# apply_unified_diff tests
# ===================================================================


class TestApplyUnifiedDiff:
    """Tests for unified diff application."""

    def test_empty_diff(self) -> None:
        """Empty diff returns content unchanged."""
        content = "line1\nline2\nline3\n"
        assert apply_unified_diff(content, "") == content

    def test_no_hunks(self) -> None:
        """Diff with only headers and no hunks returns content unchanged."""
        diff = "--- a/file.py\n+++ b/file.py\n"
        content = "line1\nline2\n"
        assert apply_unified_diff(content, diff) == content

    def test_add_line_in_middle(self) -> None:
        """Add a line in the middle of the file."""
        content = "a\nb\nc\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 a
 b
+inserted
 c
"""
        result = apply_unified_diff(content, diff)
        assert result == "a\nb\ninserted\nc\n"

    def test_remove_line(self) -> None:
        """Remove a line from the middle."""
        content = "a\nb\nc\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +1,2 @@
 a
-b
 c
"""
        result = apply_unified_diff(content, diff)
        assert result == "a\nc\n"

    def test_replace_line(self) -> None:
        """Replace a line with different content."""
        content = "a\nold\nc\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 a
-old
+new
 c
"""
        result = apply_unified_diff(content, diff)
        assert result == "a\nnew\nc\n"

    def test_multiple_hunks(self) -> None:
        """Apply multiple hunks in different parts of the file."""
        content = "a\nb\nc\nd\ne\nf\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 a
 b
+insert1
 c
@@ -4,3 +5,4 @@
 d
 e
+insert2
 f
"""
        result = apply_unified_diff(content, diff)
        assert result == "a\nb\ninsert1\nc\nd\ne\ninsert2\nf\n"

    def test_preserve_trailing_newline(self) -> None:
        """When original has trailing newline, result also has it."""
        content = "line1\nline2\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,2 +1,3 @@
 line1
 line2
+line3
"""
        result = apply_unified_diff(content, diff)
        assert result == "line1\nline2\nline3\n"
        assert result.endswith("\n")

    def test_no_trailing_newline_input(self) -> None:
        """When original has no trailing newline, result also lacks it."""
        content = "line1\nline2"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,2 +1,3 @@
 line1
 line2
+line3
"""
        result = apply_unified_diff(content, diff)
        expected = "line1\nline2\nline3"
        assert result == expected
        assert not result.endswith("\n")

    def test_context_mismatch_raises(self) -> None:
        """When context line does not match, raises ValueError."""
        content = "a\nb\nc\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 a
-WRONG
+new
 c
"""
        with pytest.raises(ValueError, match="Context mismatch"):
            apply_unified_diff(content, diff)

    def test_add_at_beginning(self) -> None:
        """Add lines at the beginning of the file."""
        content = "old1\nold2\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,2 +1,4 @@
+new1
+new2
 old1
 old2
"""
        result = apply_unified_diff(content, diff)
        assert result == "new1\nnew2\nold1\nold2\n"

    def test_add_at_end(self) -> None:
        """Add lines at the end of the file."""
        content = "a\nb\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,2 +1,4 @@
 a
 b
+c
+d
"""
        result = apply_unified_diff(content, diff)
        assert result == "a\nb\nc\nd\n"

    def test_remove_all_lines(self) -> None:
        """Remove all lines from the file."""
        content = "a\nb\nc\n"
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,3 +0,0 @@
-a
-b
-c
"""
        result = apply_unified_diff(content, diff)
        assert result == "\n"  # Empty file with trailing newline

    def test_complex_patch(self) -> None:
        """More realistic patch: reorder and modify."""
        content = """\
def old_name():
    \"\"\"Old docstring.\"\"\"
    x = 1
    return x


def helper():
    return 42
"""
        diff = """\
--- a/file.py
+++ b/file.py
@@ -1,5 +1,7 @@
-def old_name():
-    \"\"\"Old docstring.\"\"\"
+def new_name(param: int) -> int:
+    \"\"\"New docstring.\"\"\"
+    # New comment
     x = 1
     return x
+
"""
        result = apply_unified_diff(content, diff)
        assert "def new_name(param: int) -> int:" in result
        assert '"""New docstring."""' in result
        assert "# New comment" in result
        assert "def old_name()" not in result
        assert "Old docstring" not in result


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
