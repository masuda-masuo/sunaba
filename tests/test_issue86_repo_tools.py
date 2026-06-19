"""Tests for Issue #86: clone_repo and list_files tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import clone_repo, list_files


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


class TestCloneRepo:
    """Tests for clone_repo tool."""

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    def test_successful_clone(self, mock_record, mock_docker):
        """Successful clone returns ok with clone_path."""
        container = _make_container([
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        assert result["repo"] == "owner/mytool"
        assert result["clone_path"] == "/home/sandbox/mytool"
        assert result["branch"] == "default"
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    def test_clone_with_branch(self, mock_record, mock_docker):
        """Clone with branch specified."""
        container = _make_container([
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            clone_repo("abc123def456", "owner/mytool", branch="develop")
        )
        assert result["status"] == "ok"
        assert result["branch"] == "develop"
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.server._docker")
    def test_clone_failure(self, mock_docker):
        """Clone failure returns error status."""
        container = _make_container([
            (1, b"", b"fatal: repository not found\n"),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/nonexistent"))
        assert result["status"] == "error"
        assert "repository not found" in result["error"]

    @patch("code_sandbox_mcp.server._docker")
    def test_clone_with_custom_dest(self, mock_docker):
        """Clone with custom dest_dir computes correct clone_path."""
        container = _make_container([
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            clone_repo("abc123def456", "owner/mytool", dest_dir="/tmp/work")
        )
        assert result["clone_path"] == "/tmp/work/mytool"

    @patch("code_sandbox_mcp.server._docker")
    def test_invalid_repo_format(self, mock_docker):
        """Invalid repo format returns error."""
        result = json.loads(clone_repo("abc123def456", "not-a-valid-repo"))
        assert "error" in result
        assert "Invalid repo format" in result["error"]

    @patch("code_sandbox_mcp.server._docker")
    def test_container_not_found(self, mock_docker):
        """Missing container returns error."""
        client = MagicMock()
        from docker.errors import NotFound
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = json.loads(clone_repo("abc123def456", "owner/repo"))
        assert "error" in result
        assert "not found" in result["error"]


class TestListFiles:
    """Tests for list_files tool."""

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
    def test_list_empty_directory(self, mock_docker):
        """Empty directory returns empty list."""
        container = _make_container([
            (0, b"\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/root"))
        assert result["total"] == 0
        assert result["files"] == []

    @patch("code_sandbox_mcp.server._docker")
    def test_list_error(self, mock_docker):
        """Find error returns error field."""
        container = _make_container([
            (1, b"", b"find: /nonexistent: No such file or directory\n"),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/nonexistent"))
        assert "error" in result
