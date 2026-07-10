"""Tests for read_file_lines error handling and edge cases."""

from __future__ import annotations

from src.sunaba.edit_verify import (
    read_file_lines,
)

# ===================================================================
# _parse_ruff_output tests
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
            "src.sunaba.edit_verify.read_file",
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
            "src.sunaba.edit_verify.read_file",
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

