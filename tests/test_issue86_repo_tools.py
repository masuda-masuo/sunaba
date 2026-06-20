"""Tests for Issue #86: clone_repo and list_files tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import clone_repo, list_files, read_file_range


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
    def test_clone_targets_repo_subdir(self, mock_docker):
        """Issue #131: gh clones into {dest_dir}/{repo_name}, not dest_dir.

        ``gh repo clone`` treats its second argument as the clone target
        itself, so cloning into the default ``/home/sandbox`` (an existing
        non-empty home) fails.  The command must target the repo subdir.
        """
        container = _make_container([
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        clone_repo("abc123def456", "owner/mytool")

        cmd = container.exec_run.call_args[0][0][-1]
        assert "/home/sandbox/mytool" in cmd
        # The bare parent must not be the clone target.
        assert "gh repo clone 'owner/mytool' '/home/sandbox'" not in cmd

    @patch("code_sandbox_mcp.server._docker")
    def test_clone_existing_dir_adds_hint(self, mock_docker):
        """Issue #131: 'already exists' failures get an actionable hint."""
        container = _make_container([
            (
                1,
                b"",
                b"fatal: destination path '/home/sandbox/mytool' already "
                b"exists and is not an empty directory.\n",
            ),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "error"
        assert "Hint:" in result["error"]
        assert "dest_dir" in result["error"]


class TestReadFileRange:
    """Issue #131: read_file_range must not raise NameError."""

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
    def test_read_file_range_container_not_found(self, mock_docker):
        """Missing container returns a JSON error, not a raised exception."""
        client = MagicMock()
        from docker.errors import NotFound
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = json.loads(read_file_range("abc123def456", "/f.txt"))
        assert "error" in result
        assert "not found" in result["error"]

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

    @patch("code_sandbox_mcp.server._docker")
    def test_list_default_path(self, mock_docker):
        """Default path is /home/sandbox."""
        container = _make_container([
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456"))
        assert result["path"] == "/home/sandbox"
        assert result["total"] == 0
        assert result["files"] == []
