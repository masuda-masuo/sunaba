"""Pure-function tests for the edit_engine module.

No docker/container mocking -- all tests operate on plain strings.
"""

from __future__ import annotations

from sunaba.tools.edit_engine import (
    _ALREADY_APPLIED_MIN_CHARS,
    _CLASS_RE,
    _DEF_RE,
    _NEAR_MISS_DIFF_CAP,
    _NEAR_MISS_FULL_DIFF_MAX_LINES,
    _SUCCESS_ECHO_CONTEXT,
    _SUCCESS_ECHO_MAX_ROWS,
    _build_first_mismatch_report,
    _build_near_miss_echo,
    _build_success_echo,
    _extract_symbol_from_old_str,
    _find_all_matches,
    _get_line_indent,
    _is_bare_signature,
    _parses_as_definition,
    _python_syntax_note,
    _reindent_lines,
    _try_whitespace_flexible,
)

# ===================================================================
# Regex constants
# ===================================================================


class TestDefRe:
    def test_matches_def(self) -> None:
        assert _DEF_RE.match("def foo():")
        assert _DEF_RE.match("async def fetch():")

    def test_matches_class(self) -> None:
        assert _CLASS_RE.match("class Bar:")
        assert _CLASS_RE.match("  class Bar:")

    def test_no_match_non_def(self) -> None:
        assert _DEF_RE.match("x = 1") is None
        assert _CLASS_RE.match("def foo():") is None


# ===================================================================
# _extract_symbol_from_old_str
# ===================================================================


class TestExtractSymbolFromOldStr:
    def test_def_simple(self) -> None:
        assert _extract_symbol_from_old_str("def foo():") == "foo"

    def test_async_def(self) -> None:
        assert _extract_symbol_from_old_str("async def fetch():") == "fetch"

    def test_class(self) -> None:
        assert _extract_symbol_from_old_str("class Bar:") == "Bar"

    def test_decorated(self) -> None:
        assert _extract_symbol_from_old_str("@decorator" "\n" "def foo():") == "foo"

    def test_multiple_decorators(self) -> None:
        assert _extract_symbol_from_old_str("@dec1" "\n" "@dec2" "\n" "def f():") == "f"

    def test_comment_before_def(self) -> None:
        assert _extract_symbol_from_old_str("# comment" "\n" "def foo():") == "foo"

    def test_blank_lines_before_def(self) -> None:
        assert _extract_symbol_from_old_str("\n\ndef foo():") == "foo"

    def test_not_a_def(self) -> None:
        assert _extract_symbol_from_old_str("x = 1") is None

    def test_empty_string(self) -> None:
        assert _extract_symbol_from_old_str("") is None

    def test_indented_def(self) -> None:
        assert _extract_symbol_from_old_str("   def foo():") == "foo"

    def test_decorated_with_blank_lines(self) -> None:
        assert _extract_symbol_from_old_str("\n\n# some comment" "\n" "@decorator" "\n" "def foo():" "\n    pass\n") == "foo"

    def test_non_py_old_str(self) -> None:
        assert _extract_symbol_from_old_str("def foo(): # type: ignore") == "foo"

    def test_first_non_comment_non_decorator_line_wins(self) -> None:
        """If the first meaningful line is not a def/class, return None."""
        assert _extract_symbol_from_old_str("x = 1" "\n" "def foo():") is None


# ===================================================================
# _parses_as_definition
# ===================================================================


class TestParsesAsDefinition:
    def test_def_parses(self) -> None:
        assert _parses_as_definition("def foo():" "\n" "    pass") is True

    def test_async_def_parses(self) -> None:
        assert _parses_as_definition("async def foo():" "\n" "    pass") is True

    def test_class_parses(self) -> None:
        assert _parses_as_definition("class Bar:" "\n" "    pass") is True

    def test_non_code_does_not_parse(self) -> None:
        assert _parses_as_definition("x = 1") is False

    def test_syntax_error_does_not_parse(self) -> None:
        assert _parses_as_definition("def foo(:") is False  # invalid syntax

    def test_indented_code_parses(self) -> None:
        assert _parses_as_definition("    def foo():" "\n" "        pass") is True

    def test_empty_string(self) -> None:
        assert _parses_as_definition("") is False


# ===================================================================
# _is_bare_signature
# ===================================================================


class TestIsBareSignature:
    def test_bare_def_is_bare(self) -> None:
        assert _is_bare_signature("def foo():") is True

    def test_bare_async_def_is_bare(self) -> None:
        assert _is_bare_signature("async def fetch():") is True

    def test_bare_class_is_bare(self) -> None:
        assert _is_bare_signature("class Bar:") is True

    def test_decorated_bare_is_bare(self) -> None:
        assert _is_bare_signature("@decorator" "\n" "def foo():") is True

    def test_comment_before_bare_is_bare(self) -> None:
        assert _is_bare_signature("# comment" "\n" "def foo():" "\n") is True

    def test_unfinished_sig_start_is_bare(self) -> None:
        assert _is_bare_signature("def foo(") is True

    def test_multi_line_decorator_is_bare(self) -> None:
        assert _is_bare_signature(
            "@decorator(" "\n" "    arg1," "\n" "    arg2," "\n" ")" "\n" "def foo():"
        ) is True

    def test_multi_line_signature_is_bare(self) -> None:
        assert _is_bare_signature(
            "def foo(" "\n" "    a: int," "\n" "    b: str = 'x'," "\n" ") -> None:"
        ) is True

    def test_with_body_is_not_bare(self) -> None:
        assert _is_bare_signature("def foo():" "\n" "pass") is False
        assert _is_bare_signature("def foo():" "\n" "    return 1") is False

    def test_one_liner_is_not_bare(self) -> None:
        """def f(): pass is a complete definition, never bare."""
        assert _is_bare_signature("def f(): pass") is False

    def test_overload_stub_is_not_bare(self) -> None:
        """def f(): ... is a complete definition (overload stub)."""
        assert _is_bare_signature("def f(): ...") is False


# ===================================================================
# _find_all_matches
# ===================================================================


class TestFindAllMatches:
    def test_single_match(self) -> None:
        result = _find_all_matches("abc\ndef\nghi", "def")
        assert result == [(4, 2)]

    def test_multiple_matches(self) -> None:
        result = _find_all_matches("abc\ndef\nghi\ndef\n", "def")
        assert result == [(4, 2), (12, 4)]

    def test_no_match(self) -> None:
        assert _find_all_matches("abc\nghi\n", "xyz") == []

    def test_empty_text(self) -> None:
        assert _find_all_matches("", "xyz") == []

    def test_empty_pattern(self) -> None:
        """An empty pattern matches at every position (offset 0, 1, ...)."""
        result = _find_all_matches("ab", "")
        assert len(result) > 0
        assert result[0] == (0, 1)  # first match at offset 0, line 1


# ===================================================================
# _get_line_indent
# ===================================================================


class TestGetLineIndent:
    def test_no_indent(self) -> None:
        assert _get_line_indent("foo") == 0

    def test_spaces(self) -> None:
        assert _get_line_indent("    foo") == 4

    def test_tab(self) -> None:
        assert _get_line_indent("\tfoo") == 1

    def test_empty_string(self) -> None:
        assert _get_line_indent("") == 0

    def test_whitespace_only(self) -> None:
        assert _get_line_indent("   ") == 3


# ===================================================================
# _reindent_lines
# ===================================================================


class TestReindentLines:
    def test_positive_delta(self) -> None:
        lines = ["def foo():", "    pass"]
        result = _reindent_lines(lines, 4)
        assert result == ["    def foo():", "        pass"]

    def test_negative_delta(self) -> None:
        lines = ["    def foo():", "        pass"]
        result = _reindent_lines(lines, -4)
        assert result == ["def foo():", "    pass"]

    def test_negative_delta_not_enough_indent(self) -> None:
        """Remove at most the available indent."""
        lines = ["  foo", "bar"]
        result = _reindent_lines(lines, -4)
        assert result == ["foo", "bar"]

    def test_empty_lines_preserved(self) -> None:
        lines = ["def foo():", "", "    pass"]
        result = _reindent_lines(lines, 4)
        assert result == ["    def foo():", "", "        pass"]

    def test_zero_delta(self) -> None:
        lines = ["def foo():", "    pass"]
        assert _reindent_lines(lines, 0) == lines


# ===================================================================
# _try_whitespace_flexible
# ===================================================================


class TestTryWhitespaceFlexible:
    def test_exact_match(self) -> None:
        existing = "def foo():" "\n" "    return 1" "\n"
        result = _try_whitespace_flexible(existing, "def foo():", "def bar():")
        assert result is not None
        content, start, end = result
        assert "def bar():" in content
        assert start == 1

    def test_indent_mismatch(self) -> None:
        """Whitespace-flexible matching handles different indentation."""
        existing = "    def foo():" "\n" "        return 1" "\n"
        result = _try_whitespace_flexible(existing, "def foo():", "def bar():")
        assert result is not None
        content, start, end = result
        assert "    def bar():" in content

    def test_no_match(self) -> None:
        existing = "line1" "\n" "line2" "\n"
        result = _try_whitespace_flexible(existing, "xxx", "yyy")
        assert result is None

    def test_ambiguous(self) -> None:
        """Multiple matches return an error string."""
        existing = "a" "\n" "b" "\n" "a" "\n" "b" "\n"
        result = _try_whitespace_flexible(existing, "a", "c")
        assert isinstance(result, str)
        assert "Error" in result

    def test_old_str_longer_than_file(self) -> None:
        existing = "short" "\n"
        result = _try_whitespace_flexible(existing, "a" "\n" "b" "\n" "c", "x")
        assert result is None

    def test_trailing_newline_preserved(self) -> None:
        existing = "a" "\n" "b" "\n"
        result = _try_whitespace_flexible(existing, "b", "c")
        assert result is not None
        content, _, _ = result
        assert content.endswith("\n")

    def test_zero_delta_reindent(self) -> None:
        existing = "def foo():" "\n" "    return 1" "\n"
        result = _try_whitespace_flexible(existing, "def foo():" "\n" "    return 1", "def foo():" "\n" "    return 2")
        assert result is not None
        content, _, _ = result
        assert "return 2" in content


# ===================================================================
# _build_first_mismatch_report
# ===================================================================


class TestBuildFirstMismatchReport:
    def test_identical_lines(self) -> None:
        assert _build_first_mismatch_report(["a", "b"], ["a", "b"], 0) == ""

    def test_replace_mismatch(self) -> None:
        result = _build_first_mismatch_report(["a", "x"], ["a", "b"], 10)
        assert "First mismatch" in result
        assert "old_str" in result
        assert "file line 12" in result  # 10 + 1 + 1

    def test_delete_mismatch(self) -> None:
        result = _build_first_mismatch_report(["a", "b", "c"], ["a", "b"], 0)
        assert "First mismatch" in result
        assert "no counterpart" in result

    def test_insert_mismatch(self) -> None:
        result = _build_first_mismatch_report(["a"], ["a", "b"], 5)
        assert "First mismatch" in result
        assert "no counterpart in old_str" in result


# ===================================================================
# _build_near_miss_echo
# ===================================================================


class TestBuildNearMissEcho:
    def test_near_miss_format(self) -> None:
        """Near-miss echo includes error, context, and diff."""
        existing = "def foo():" "\n" "    return 1" "\n"
        result = _build_near_miss_echo(existing, "def bar():" "\n" "    return 2", "/tmp/test.py")
        assert "Error: old_str not found" in result
        assert "/tmp/test.py" in result
        assert "Best matching region" in result
        assert "Unified diff" in result

    def test_very_different_content(self) -> None:
        existing = "aaa" "\n" "bbb" "\n" "ccc" "\n"
        result = _build_near_miss_echo(existing, "xxx", "/f.py")
        assert "Error" in result

    def test_identical_whitespace_differs(self) -> None:
        existing = "def foo():" "\n" "    return 1" "\n"
        result = _build_near_miss_echo(existing, "def foo():" "\n" "  return 1", "/f.py")
        # The diff is not empty (whitespace varies), so the
        # "(identical content, whitespace differs)" message is not
        # emitted -- instead a unified diff is shown.
        assert "def foo():" in result
        assert "return 1" in result

    def test_long_old_str_capped(self) -> None:
        """Long old_str blocks cap the diff at _NEAR_MISS_DIFF_CAP."""
        old_lines = [f"line{i}" for i in range(60)]
        existing_lines = [f"  line{i}" for i in range(60)]
        result = _build_near_miss_echo(
            "\n".join(existing_lines),
            "\n".join(old_lines),
            "/f.py",
        )
        assert "truncated" in result


# ===================================================================
# _build_success_echo
# ===================================================================


class TestBuildSuccessEcho:
    def test_single_line(self) -> None:
        result = _build_success_echo("before" "\n" "replaced" "\n" "after", "/f.py", 2, 2)
        assert "Written" in result
        assert "replaced line 2" in result
        assert ">>>" in result

    def test_multi_line(self) -> None:
        result = _build_success_echo(
            "a" "\n" "b" "\n" "c" "\n" "d" "\n" "e", "/f.py", 2, 4,
        )
        assert "replaced lines 2-4" in result

    def test_empty_content(self) -> None:
        result = _build_success_echo("", "/f.py", 1, 1)
        assert "Written 0 bytes" in result

    def test_context_lines_shown(self) -> None:
        content = "\n".join(f"line{i}" for i in range(1, 21))
        result = _build_success_echo(content, "/f.py", 10, 12)
        # Should have context lines (8-14) plus markers
        assert "line8" in result
        assert "line14" in result
        # Should NOT show distant lines.
        # Note: "line1" matches "line10/line11/...", so check more precisely.
        assert " 1 | line1" not in result
        assert "line20" not in result

    def test_long_echo_middle_elided(self) -> None:
        """Very large replaced region elides the middle."""
        lines = [f"line{i}" for i in range(50)]
        content = "\n".join(lines)
        result = _build_success_echo(content, "/f.py", 5, 45)
        assert "... " in result and "lines)" in result


# ===================================================================
# _python_syntax_note
# ===================================================================


class TestPythonSyntaxNote:
    def test_non_py_file(self) -> None:
        assert _python_syntax_note("/tmp/f.txt", "not python") == ""

    def test_valid_python(self) -> None:
        assert _python_syntax_note("/tmp/f.py", "x = 1") == ""

    def test_invalid_python(self) -> None:
        result = _python_syntax_note("/tmp/f.py", "def foo(:")
        assert "Warning" in result
        assert "/tmp/f.py" in result

    def test_invalid_python_class_no_body(self) -> None:
        result = _python_syntax_note("/workspace/test.py", "class Foo")
        assert "Warning" in result

    def test_valid_class_definition(self) -> None:
        result = _python_syntax_note("/workspace/test.py", "class Foo:\n    pass")
        assert result == ""  # valid: class with body


# ===================================================================
# Constants (sanity checks)
# ===================================================================


class TestConstants:
    def test_near_miss_values(self) -> None:
        assert _NEAR_MISS_FULL_DIFF_MAX_LINES == 50
        assert _NEAR_MISS_DIFF_CAP == 30

    def test_success_echo_values(self) -> None:
        assert _SUCCESS_ECHO_CONTEXT == 2
        assert _SUCCESS_ECHO_MAX_ROWS == 30

    def test_already_applied_min_chars(self) -> None:
        assert _ALREADY_APPLIED_MIN_CHARS == 8
