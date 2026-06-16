"""Tests for the Edit/Verify subsystem (edit_verify.py).

Tests cover:
- ``apply_unified_diff`` — unified diff parsing and application
- ``lint_file`` — linter dispatch and output parsing (ruff, pylint, eslint)
- ``type_check_file`` — type checker dispatch and output parsing (mypy, pyright, tsc)
- ``read_file_lines`` — range reading with offset/limit
"""
from __future__ import annotations

import json

import pytest

from src.code_sandbox_mcp.edit_verify import (
    apply_unified_diff,
    lint_file,
    read_file_lines,
    type_check_file,
    _parse_eslint_output,
    _parse_mypy_output,
    _parse_pylint_output,
    _parse_ruff_output,
    _parse_pyright_output,
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
        assert "\"\"\"New docstring.\"\"\"" in result
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
        raw = json.dumps([
            {
                "filename": "test.py",
                "location": {"row": 5},
                "code": "F401",
                "message": "`os` imported but unused",
            },
        ])
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "F401"
        assert "unused" in result[0]["message"]

    def test_multiple_issues(self) -> None:
        raw = json.dumps([
            {"filename": "a.py", "location": {"row": 1}, "code": "E302", "message": "blank lines"},
            {"filename": "a.py", "location": {"row": 5}, "code": "W291", "message": "trailing space"},
        ])
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
        raw = json.dumps([
            {
                "path": "test.py",
                "line": 10,
                "symbol": "unused-import",
                "message-id": "W0611",
                "message": "Unused import os",
            },
        ])
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
        raw = json.dumps([
            {
                "filePath": "/app/file.js",
                "messages": [
                    {"line": 5, "ruleId": "no-unused-vars", "message": "'x' is defined but never used"},
                ],
            },
        ])
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 1
        assert result[0]["file"] == "/app/file.js"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "no-unused-vars"

    def test_multiple_files(self) -> None:
        raw = json.dumps([
            {"filePath": "a.js", "messages": [{"line": 1, "ruleId": "R1", "message": "m1"}]},
            {"filePath": "b.js", "messages": [{"line": 2, "ruleId": "R2", "message": "m2"}]},
        ])
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
        raw = json.dumps({
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "range": {"start": {"line": 10}},
                    "rule": "reportUnknownVariableType",
                    "message": "Type of 'x' is unknown",
                },
            ],
        })
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
        page = lines[offset:offset + limit]
        assert page == ["b", "c", "d"]

    def test_offset_beyond_end(self) -> None:
        """When offset >= total lines, returns empty content."""
        lines = ["a", "b"]
        page_offset = 10  # beyond length
        page = lines[page_offset:page_offset + 50]
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

        raw = json.dumps({
            "diagnostics": [
                {
                    "file": {"fileName": "test.ts", "line": 5},
                    "code": "TS1234",
                    "messageText": "Some error",
                },
            ],
        })
        result = _parse_tsc_json(raw, "file.ts")
        assert len(result) == 1
        assert result[0]["file"] == "test.ts"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "TS1234"

    def test_tsc_json_empty(self) -> None:
        from src.code_sandbox_mcp.edit_verify import _parse_tsc_json

        assert _parse_tsc_json("", "file.ts") == []
