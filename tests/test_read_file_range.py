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
