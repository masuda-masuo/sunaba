"""Tests for the output control module.

Tests cover:
- ``strip_ansi`` — ANSI escape code removal
- ``strip_carriage_returns`` — ``\\r`` progress bar collapsing
- ``strip_timestamps`` — ISO-8601 timestamp removal
- ``sanitize_output`` — combined sanitization
- ``compress_repeated_lines`` — ``[\u00d7N]`` compression
- ``truncate_output`` — verbosity modes (error_only, summary, full)
- ``truncate_output`` — truncation metadata
- ``paginate_output`` — offset / limit / has_more / next_offset
"""
from __future__ import annotations

import pytest

from code_sandbox_mcp.output_control import (
    OutputMetadata,
    PageResult,
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    strip_ansi,
    strip_carriage_returns,
    strip_timestamps,
    truncate_output,
)


# =======================================================================
# strip_ansi
# =======================================================================


class TestStripAnsi:
    """Tests for ANSI escape code removal."""

    def test_strips_color_codes(self) -> None:
        raw = "\x1b[31mred\x1b[0m"
        assert strip_ansi(raw) == "red"

    def test_strips_bold(self) -> None:
        raw = "\x1b[1mbold\x1b[22m"
        assert strip_ansi(raw) == "bold"

    def test_strips_cursor_movement(self) -> None:
        raw = "\x1b[2J\x1b[Hclear"
        assert strip_ansi(raw) == "clear"

    def test_preserves_plain_text(self) -> None:
        raw = "hello world"
        assert strip_ansi(raw) == "hello world"

    def test_strips_multiple_sequences(self) -> None:
        raw = "\x1b[32mgreen\x1b[33myellow\x1b[0m"
        assert strip_ansi(raw) == "greenyellow"

    def test_empty_string(self) -> None:
        assert strip_ansi("") == ""


# =======================================================================
# strip_carriage_returns
# =======================================================================


class TestStripCarriageReturns:
    """Tests for \\r-based progress bar collapsing."""

    def test_keeps_last_progress_state(self) -> None:
        raw = "Downloading... 10%\rDownloading... 50%\rDownloading... 100%\n"
        assert strip_carriage_returns(raw) == "Downloading... 100%\n"

    def test_preserves_lines_without_cr(self) -> None:
        raw = "line1\nline2\nline3\n"
        assert strip_carriage_returns(raw) == "line1\nline2\nline3\n"

    def test_empty_string(self) -> None:
        assert strip_carriage_returns("") == ""

    def test_mixed_cr_and_newlines(self) -> None:
        raw = "step1\rstep2\nline after\nprogress\rprogress done\n"
        result = strip_carriage_returns(raw)
        assert result == "step2\nline after\nprogress done\n"

    def test_only_cr_no_newline(self) -> None:
        raw = "a\rb\rc"
        assert strip_carriage_returns(raw) == "c"


# =======================================================================
# strip_timestamps
# =======================================================================


class TestStripTimestamps:
    """Tests for ISO-8601 timestamp removal."""

    def test_removes_timestamp_with_z(self) -> None:
        raw = "2026-06-17T12:34:56.789Z command output\n"
        assert strip_timestamps(raw) == "command output\n"

    def test_removes_timestamp_without_z(self) -> None:
        raw = "2026-06-17T12:34:56.789 command output\n"
        assert strip_timestamps(raw) == "command output\n"

    def test_removes_timestamp_with_comma(self) -> None:
        raw = "2026-06-17T12:34:56,789Z output\n"
        assert strip_timestamps(raw) == "output\n"

    def test_preserves_text_without_timestamp(self) -> None:
        raw = "hello world\n"
        assert strip_timestamps(raw) == "hello world\n"

    def test_empty_string(self) -> None:
        assert strip_timestamps("") == ""

    def test_multiple_lines(self) -> None:
        raw = (
            "2026-06-17T12:34:56.789Z first\n"
            "2026-06-17T12:34:57.123Z second\n"
        )
        assert strip_timestamps(raw) == "first\nsecond\n"


# =======================================================================
# sanitize_output
# =======================================================================


class TestSanitizeOutput:
    """Tests for combined output sanitization."""

    def test_removes_ansi_and_timestamps(self) -> None:
        raw = "\x1b[32m2026-06-17T12:34:56.789Z hello\x1b[0m\n"
        assert sanitize_output(raw) == "hello\n"

    def test_removes_cr_and_ansi(self) -> None:
        raw = "\x1b[34mloading... 50%\r\x1b[34mloading... 100%\x1b[0m\n"
        assert sanitize_output(raw) == "loading... 100%\n"

    def test_preserves_clean_text(self) -> None:
        raw = "hello\nworld\n"
        assert sanitize_output(raw) == "hello\nworld\n"

    def test_empty_string(self) -> None:
        assert sanitize_output("") == ""


# =======================================================================
# compress_repeated_lines
# =======================================================================


class TestCompressRepeatedLines:
    """Tests for consecutive repeated line compression."""

    def test_compresses_repeated_lines(self) -> None:
        raw = "retrying...\nretrying...\nretrying...\n"
        result = compress_repeated_lines(raw)
        assert result == "[\u00d73] retrying...\n"

    def test_preserves_unique_lines(self) -> None:
        raw = "first\nsecond\nthird\n"
        assert compress_repeated_lines(raw) == "first\nsecond\nthird\n"

    def test_compresses_at_end(self) -> None:
        raw = "start\ndone\ndone\n"
        assert compress_repeated_lines(raw) == "start\n[\u00d72] done\n"

    def test_compresses_at_start(self) -> None:
        raw = "loading\nloading\ndone\n"
        assert compress_repeated_lines(raw) == "[\u00d72] loading\ndone\n"

    def test_empty_string(self) -> None:
        assert compress_repeated_lines("") == ""

    def test_single_line(self) -> None:
        assert compress_repeated_lines("only") == "only"

    def test_no_repetition(self) -> None:
        raw = "a\nb\na\n"  # Not consecutive, should not be compressed
        assert compress_repeated_lines(raw) == "a\nb\na\n"


# =======================================================================
# truncate_output
# =======================================================================


class TestTruncateOutput:
    """Tests for output truncation with verbosity levels."""

    def test_verbose_full_shows_all(self) -> None:
        text = "line1\nline2\nline3\n"
        display, meta = truncate_output(text, verbose="full")
        assert display == text
        assert meta.shown == 3
        assert meta.total_lines == 3
        assert meta.truncated is False

    def test_verbose_error_only_hides_success(self) -> None:
        text = "success output\n"
        display, meta = truncate_output(
            text, verbose="error_only", exit_code=0, stderr=""
        )
        assert display == ""
        assert meta.shown == 0

    def test_verbose_error_only_shows_on_error(self) -> None:
        text = "error output\n"
        display, meta = truncate_output(
            text, verbose="error_only", exit_code=1, stderr="something wrong"
        )
        assert display == "error output\n"
        assert meta.shown == 1

    def test_verbose_error_only_shows_on_stderr(self) -> None:
        text = "some output\n"
        display, meta = truncate_output(
            text, verbose="error_only", exit_code=0, stderr="warning"
        )
        assert display == "some output\n"
        assert meta.shown == 1

    def test_summary_no_truncation_small_output(self) -> None:
        text = "line1\nline2\n"
        display, meta = truncate_output(text, max_lines=100, verbose="summary")
        assert display == text
        assert meta.truncated is False
        assert meta.shown == 2

    def test_summary_truncates_large_output(self) -> None:
        # Create 20 lines
        lines = [f"line{i}" for i in range(20)]
        text = "\n".join(lines)
        display, meta = truncate_output(text, max_lines=6, verbose="summary")
        assert meta.truncated is True
        # 3 head + 1 omission + 3 tail = 7 lines shown
        assert meta.shown == 7
        assert "lines omitted" in display

    def test_error_only_tail_context_on_error(self) -> None:
        # Create 20 lines
        lines = [f"line{i}" for i in range(20)]
        text = "\n".join(lines)
        display, meta = truncate_output(
            text, max_lines=5, verbose="error_only", exit_code=1, stderr="err"
        )
        # Should show last 5 lines (tail)
        assert meta.truncated is True
        assert meta.shown == 5
        assert "line15" in display
        assert "line19" in display
        assert "line0" not in display  # Head should not be in tail

    def test_empty_text_returns_empty(self) -> None:
        display, meta = truncate_output("", verbose="summary")
        assert display == ""
        assert meta.shown == 0
        assert meta.total_lines == 0

    def test_whitespace_only_returns_empty(self) -> None:
        display, meta = truncate_output("   \n  \n", verbose="summary")
        assert display == ""
        assert meta.shown == 0


# =======================================================================
# paginate_output
# =======================================================================


class TestPaginateOutput:
    """Tests for output pagination."""

    def test_first_page_with_limit(self) -> None:
        lines = "\n".join([f"line{i}" for i in range(10)])
        page = paginate_output(lines, offset=0, limit=3)
        assert page.content == "line0\nline1\nline2"
        assert page.next_offset == 3
        assert page.has_more is True

    def test_middle_page(self) -> None:
        lines = "\n".join([f"line{i}" for i in range(10)])
        page = paginate_output(lines, offset=3, limit=3)
        assert page.content == "line3\nline4\nline5"
        assert page.next_offset == 6
        assert page.has_more is True

    def test_last_page_no_more(self) -> None:
        lines = "\n".join([f"line{i}" for i in range(10)])
        page = paginate_output(lines, offset=9, limit=3)
        assert page.content == "line9"
        assert page.next_offset is None
        assert page.has_more is False

    def test_offset_beyond_end(self) -> None:
        lines = "line0\nline1\n"
        page = paginate_output(lines, offset=10, limit=3)
        assert page.content == ""
        assert page.has_more is False

    def test_empty_text(self) -> None:
        page = paginate_output("", offset=0, limit=10)
        assert page.content == ""
        assert page.has_more is False

    def test_limit_exceeds_remaining(self) -> None:
        lines = "\n".join([f"line{i}" for i in range(5)])
        page = paginate_output(lines, offset=3, limit=10)
        assert page.content == "line3\nline4"
        assert page.next_offset is None
        assert page.has_more is False

    def test_zero_offset_is_beginning(self) -> None:
        lines = "first\nsecond\n"
        page = paginate_output(lines, offset=0, limit=1)
        assert page.content == "first"
        assert page.next_offset == 1
        assert page.has_more is True
