"""Tests for read_file_range and list_files tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.file import list_files, read_file_range


def _make_container(exec_returns):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client(container):
    client = MagicMock()
    client.containers.get.return_value = container
    return client


class TestReadFileRange:
    """Issue #131: read_file_range must not raise NameError."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_read_file_range_no_nameerror(self, mock_docker):
        """Regression: 'name container is not defined' must not recur.

        Previously read_file_range referenced an undefined ``container``
        variable, failing every call with ``NameError``.  It must read the
        requested lines via the resolved container object instead.
        """
        file_body = "line0\nline1\nline2\nline3\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/home/sandbox/f.txt", offset=1, limit=2)
        )
        assert "error" not in result or result["error"] is None
        assert result["content"] == "line1\nline2"
        assert result["total_lines"] == 5  # trailing newline -> empty final line

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_read_file_range_container_not_found(self, mock_docker):
        """Missing container returns a JSON error, not a raised exception."""
        client = MagicMock()
        from docker.errors import NotFound
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = json.loads(read_file_range("abc123def456", "/f.txt"))
        assert "error" in result
        assert "not found" in result["error"]




class TestReadFileRangeStartEndLine:
    """Issue #386: start_line/end_line params must work correctly."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_start_line_end_line_both_specified(self, mock_docker):
        """start_line=2, end_line=3 returns lines 2-3 (1-indexed, inclusive)."""
        file_body = "line0\nline1\nline2\nline3\nline4\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=2, end_line=3)
        )
        assert result["error"] is None
        assert result["content"] == "line1\nline2"
        assert result["shown"] == 2
        assert result["total_lines"] == 6

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_start_line_only_reads_to_end(self, mock_docker):
        """start_line=3 (no end_line) reads from line 3 to end."""
        file_body = "line0\nline1\nline2\nline3\nline4\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=3)
        )
        assert result["error"] is None
        assert result["content"] == "line2\nline3\nline4\n"
        assert result["shown"] == 4  # trailing newline -> empty final line
        assert result["total_lines"] == 6

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_start_line_end_line_single_line(self, mock_docker):
        """start_line=1, end_line=1 returns exactly one line."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=1, end_line=1)
        )
        assert result["error"] is None
        assert result["content"] == "line0"
        assert result["shown"] == 1


    @patch("code_sandbox_mcp.tools.file._docker")
    def test_start_line_and_offset_mutually_exclusive(self, mock_docker):
        """start_line and non-zero offset together raise error."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=2, offset=3)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_start_line_zero_rejected(self, mock_docker):
        """start_line=0 is rejected (must be >= 1)."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=0)
        )
        assert "error" in result
        assert "must be >= 1" in result["error"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_end_line_less_than_start_line_rejected(self, mock_docker):
        """end_line < start_line is rejected."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=3, end_line=1)
        )
        assert "error" in result
        assert "end_line must be >= start_line" in result["error"]


    @patch("code_sandbox_mcp.tools.file._docker")
    def test_offset_limit_still_works(self, mock_docker):
        """Backward compatibility: offset/limit still function."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", offset=0, limit=2)
        )
        assert result["error"] is None
        assert result["content"] == "line0\nline1"
        assert result["shown"] == 2


class TestListFiles:
    """Tests for list_files tool."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_successful_list(self, mock_docker):
        """Successful listing returns file paths."""
        files = (
            "/root/file1.py\n"
            "/root/subdir/file2.py\n"
            "/root/subdir/file3.md\n"
        )
        container = _make_container([
            (0, files.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/root"))
        assert result["total"] == 3
        assert "/root/file1.py" in result["files"]
        assert "/root/subdir/file2.py" in result["files"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_list_with_pattern(self, mock_docker):
        """List with glob pattern filter."""
        py_files = "/root/file1.py\n/root/file2.py\n"
        container = _make_container([
            (0, py_files.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            list_files("abc123def456", "/root", pattern="*.py")
        )
        assert result["total"] == 2

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_list_empty_directory(self, mock_docker):
        """Empty directory returns empty list."""
        container = _make_container([
            (0, b"\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/root"))
        assert result["total"] == 0
        assert result["files"] == []

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_list_error(self, mock_docker):
        """Find error returns error field."""
        container = _make_container([
            (1, b"", b"find: /nonexistent: No such file or directory\n"),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/nonexistent"))
        assert "error" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_list_default_path(self, mock_docker):
        """Default path is /home/sandbox."""
        container = _make_container([
            (0, b"", b""),
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456"))
        assert result["path"] == "/home/sandbox"
        assert result["total"] == 0
        assert result["files"] == []
